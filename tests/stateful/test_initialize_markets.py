import math

import brownie
import pytest
from brownie.convert.datatypes import Wei
from brownie.network.state import Chain
from scripts.config import CurrencyDefaults
from scripts.deployment import TestEnvironment
from tests.constants import RATE_PRECISION, SECONDS_IN_QUARTER, SECONDS_IN_YEAR
from tests.helpers import get_tref
from tests.stateful.invariants import check_system_invariants

chain = Chain()
INITIAL_CASH_AMOUNT = 100000e8


@pytest.fixture(scope="module", autouse=True)
def environment(accounts):
    env = TestEnvironment(accounts[0])
    env.enableCurrency("DAI", CurrencyDefaults)

    cToken = env.cToken["DAI"]
    token = env.token["DAI"]
    token.approve(cToken.address, 2 ** 255, {"from": accounts[0]})
    cToken.mint(10000000e18, {"from": accounts[0]})
    cToken.approve(env.proxy.address, 2 ** 255, {"from": accounts[0]})

    # Set the blocktime to the begnning of the next tRef otherwise the rates will blow up
    blockTime = chain.time()
    newTime = get_tref(blockTime) + SECONDS_IN_QUARTER + 1
    chain.mine(1, timestamp=newTime)

    return env


@pytest.fixture(autouse=True)
def isolation(fn_isolation):
    pass


def initialize_markets(environment, accounts):
    currencyId = 2
    environment.router["Governance"].updatePerpetualDepositParameters(
        currencyId, [0.4e8, 0.6e8], [0.8e9, 0.8e9]
    )

    environment.router["Governance"].updateInitializationParameters(
        currencyId, [1.01e9, 1.021e9], [0.5e9, 0.5e9]
    )

    environment.router["MintPerpetual"].perpetualTokenMint(
        currencyId, 100000e8, False, {"from": accounts[0]}
    )
    environment.router["InitializeMarkets"].initializeMarkets(currencyId, True)


def get_maturities(index):
    blockTime = chain.time()
    tRef = blockTime - blockTime % SECONDS_IN_QUARTER
    maturity = []
    if index >= 1:
        maturity.append(tRef + SECONDS_IN_QUARTER)

    if index >= 2:
        maturity.append(tRef + 2 * SECONDS_IN_QUARTER)

    if index >= 3:
        maturity.append(tRef + SECONDS_IN_YEAR)

    if index >= 4:
        maturity.append(tRef + 2 * SECONDS_IN_YEAR)

    if index >= 5:
        maturity.append(tRef + 5 * SECONDS_IN_YEAR)

    if index >= 6:
        maturity.append(tRef + 7 * SECONDS_IN_YEAR)

    if index >= 7:
        maturity.append(tRef + 10 * SECONDS_IN_YEAR)

    if index >= 8:
        maturity.append(tRef + 15 * SECONDS_IN_YEAR)

    if index >= 9:
        maturity.append(tRef + 20 * SECONDS_IN_YEAR)

    return maturity


def interpolate_market_rate(a, b, isSixMonth=False):
    shortMaturity = a[1]
    longMaturity = b[1]
    shortRate = a[6]
    longRate = b[6]

    if isSixMonth:
        return math.trunc(
            abs(
                (longRate - shortRate) * SECONDS_IN_QUARTER / (longMaturity - shortMaturity)
                + shortRate
            )
        )
    else:
        return math.trunc(
            abs(
                (longRate - shortRate)
                * (longMaturity + SECONDS_IN_QUARTER - shortMaturity)
                / (longMaturity - shortMaturity)
                + shortRate
            )
        )


def perp_token_asserts(environment, currencyId, isFirstInit, accounts, wasInit=True):
    blockTime = chain.time()
    perpTokenAddress = environment.router["Views"].getPerpetualTokenAddress(currencyId)
    (cashBalance, perpTokenBalance, lastMintTime) = environment.router["Views"].getAccountBalance(
        currencyId, perpTokenAddress
    )

    (cashGroup, assetRate) = environment.router["Views"].getCashGroupAndRate(currencyId)
    portfolio = environment.router["Views"].getAccountPortfolio(perpTokenAddress)
    (depositShares, leverageThresholds) = environment.router["Views"].getPerpetualDepositParameters(
        currencyId
    )
    (rateAnchors, proportions) = environment.router["Views"].getInitializationParameters(currencyId)
    maturity = get_maturities(cashGroup[0])
    markets = environment.router["Views"].getActiveMarkets(currencyId)
    previousMarkets = environment.router["Views"].getActiveMarketsAtBlockTime(
        currencyId, blockTime - SECONDS_IN_QUARTER
    )

    # assert perp token has no cash left
    assert cashBalance == 0
    assert perpTokenBalance == 0
    assert lastMintTime == 0

    # assert that perp token has liquidity tokens
    assert len(portfolio) == cashGroup[0]  # max market index

    # These values are used to calculate non first init liquidity values
    totalAssetCashInMarkets = sum([m[3] for m in markets])

    for (i, asset) in enumerate(portfolio):
        assert asset[0] == currencyId
        # assert liquidity token is on a valid maturity date
        assert asset[1] == maturity[i]
        # assert liquidity tokens are ordered
        assert asset[2] == 2 + i
        # assert that liquidity is proportional to deposit shares

        if isFirstInit:
            # Initialize amount is a percentage of the initial cash amount
            assert asset[3] == INITIAL_CASH_AMOUNT * depositShares[i] / int(1e8)
        elif wasInit:
            # Initialize amount is a percentage of the net cash amount
            assert asset[3] == totalAssetCashInMarkets * depositShares[i] / 1e8

    ifCashAssets = environment.router["Views"].getifCashAssets(perpTokenAddress)
    assert len(ifCashAssets) >= len(portfolio)
    for (i, asset) in enumerate(ifCashAssets):
        assert asset[0] == currencyId
        assert asset[1] == maturity[i]
        assert asset[2] == 1
        # assert that perp token has an fCash asset
        # TODO: this should be a combination of previous fCash value, and the net added
        # TODO: it's possible for this to be zero
        assert asset[3] < 0

    for (i, market) in enumerate(markets):
        assert market[1] == maturity[i]
        # all market liquidity is from the perp token
        assert market[4] == portfolio[i][3]

        totalCashUnderlying = (market[3] * Wei(1e8) * assetRate[1]) / (assetRate[2] * Wei(1e18))
        proportion = int(market[2] * RATE_PRECISION / (totalCashUnderlying + market[2]))
        # assert that market proportions are not above leverage thresholds
        assert proportion < leverageThresholds[i]

        # Ensure that fCash is greater than zero
        assert market[3] > 0

        if previousMarkets[i][6] == 0:
            # This means that the market is initialized for the first time
            assert pytest.approx(proportion, abs=2) == proportions[i]
        elif i == 0:
            # The 3 month market should have the same implied rate as the old 6 month
            assert market[5] == previousMarkets[1][5]
        elif i == 1:
            # In any other scenario then the market's oracleRate must be in line with
            # the oracle rate provided by the previous markets, this is a special case
            # for the 6 month market
            if len(previousMarkets) >= 3 and previousMarkets[2][6] != 0:
                # In this case we can interpolate between the old 6 month and 1yr
                computedOracleRate = interpolate_market_rate(
                    previousMarkets[1], previousMarkets[2], isSixMonth=True
                )
                assert pytest.approx(market[5], abs=2) == computedOracleRate
                assert pytest.approx(market[6], abs=2) == computedOracleRate
            else:
                # In this case then the proportion is set by governance (there is no
                # future rate to interpolate against)
                assert pytest.approx(proportion, abs=2) == proportions[i]
        else:
            # In this scenario the market is interpolated against the previous two rates
            computedOracleRate = interpolate_market_rate(previousMarkets[i - 1], previousMarkets[i])
            assert pytest.approx(market[5], abs=2) == computedOracleRate
            assert pytest.approx(market[6], abs=2) == computedOracleRate

    accountContext = environment.router["Views"].getAccountContext(perpTokenAddress)
    assert accountContext[0] < get_tref(blockTime) + SECONDS_IN_QUARTER
    assert not accountContext[1]
    assert accountContext[2] == currencyId

    check_system_invariants(environment, accounts)


def test_first_initialization(environment, accounts):
    currencyId = 2
    with brownie.reverts("IM: insufficient cash"):
        # no parameters are set
        environment.router["InitializeMarkets"].initializeMarkets(currencyId, True)

    environment.router["Governance"].updatePerpetualDepositParameters(
        currencyId, [0.4e8, 0.6e8], [0.8e9, 0.8e9]
    )

    environment.router["Governance"].updateInitializationParameters(
        currencyId, [1.02e9, 1.02e9], [0.5e9, 0.5e9]
    )

    with brownie.reverts("IM: insufficient cash"):
        # no cash deposits
        environment.router["InitializeMarkets"].initializeMarkets(currencyId, True)

    environment.router["MintPerpetual"].perpetualTokenMint(
        currencyId, 100000e8, False, {"from": accounts[0]}
    )
    environment.router["InitializeMarkets"].initializeMarkets(currencyId, True)
    perp_token_asserts(environment, currencyId, True, accounts)


def test_settle_and_initialize(environment, accounts):
    initialize_markets(environment, accounts)
    currencyId = 2
    blockTime = chain.time()
    chain.mine(1, timestamp=(blockTime + SECONDS_IN_QUARTER))

    # No trading has occured
    environment.router["InitializeMarkets"].initializeMarkets(currencyId, False)
    perp_token_asserts(environment, currencyId, False, accounts)


def test_settle_and_extend(environment, accounts):
    initialize_markets(environment, accounts)
    currencyId = 2

    cashGroup = list(environment.router["Views"].getCashGroup(currencyId))
    # Enable the one year market
    cashGroup[0] = 3
    cashGroup[7] = CurrencyDefaults["tokenHaircut"][0:3]
    cashGroup[8] = CurrencyDefaults["rateScalar"][0:3]
    environment.router["Governance"].updateCashGroup(currencyId, cashGroup)

    environment.router["Governance"].updatePerpetualDepositParameters(
        currencyId, [0.4e8, 0.4e8, 0.2e8], [0.8e9, 0.8e9, 0.8e9]
    )

    environment.router["Governance"].updateInitializationParameters(
        currencyId, [1.01e9, 1.021e9, 1.07e9], [0.5e9, 0.5e9, 0.5e9]
    )

    blockTime = chain.time()
    chain.mine(1, timestamp=(blockTime + SECONDS_IN_QUARTER))

    environment.router["InitializeMarkets"].initializeMarkets(currencyId, False)
    perp_token_asserts(environment, currencyId, False, accounts)

    # Test re-initialization the second time
    blockTime = chain.time()
    chain.mine(1, timestamp=(blockTime + SECONDS_IN_QUARTER))

    environment.router["InitializeMarkets"].initializeMarkets(currencyId, False)
    perp_token_asserts(environment, currencyId, False, accounts)


def test_mint_after_markets_initialized(environment, accounts):
    initialize_markets(environment, accounts)
    currencyId = 2

    marketsBefore = environment.router["Views"].getActiveMarkets(currencyId)
    tokensToMint = environment.router["Views"].calculatePerpetualTokensToMint(currencyId, 100000e8)
    (cashBalanceBefore, perpTokenBalanceBefore, lastMintTimeBefore) = environment.router[
        "Views"
    ].getAccountBalance(currencyId, accounts[0])

    environment.router["MintPerpetual"].perpetualTokenMint(
        currencyId, 100000e8, False, {"from": accounts[0]}
    )
    perp_token_asserts(environment, currencyId, False, accounts, wasInit=False)
    # Assert that no assets in portfolio
    assert len(environment.router["Views"].getAccountPortfolio(accounts[0])) == 0

    marketsAfter = environment.router["Views"].getActiveMarkets(currencyId)
    (cashBalanceAfter, perpTokenBalanceAfter, lastMintTimeAfter) = environment.router[
        "Views"
    ].getAccountBalance(currencyId, accounts[0])

    # assert increase in market liquidity
    assert len(marketsBefore) == len(marketsAfter)
    for (i, m) in enumerate(marketsBefore):
        assert m[4] < marketsAfter[i][4]

    # assert account balances are in line
    assert cashBalanceBefore == cashBalanceAfter
    assert perpTokenBalanceAfter == perpTokenBalanceBefore + tokensToMint
    assert lastMintTimeAfter > lastMintTimeBefore


@pytest.mark.skip
def test_redeem_and_sell_to_cash(environment, accounts):
    initialize_markets(environment, accounts)
    currencyId = 2

    (cashBalanceBefore, perpTokenBalanceBefore, lastMintTimeBefore) = environment.router[
        "Views"
    ].getAccountBalance(currencyId, accounts[0])
    marketsBefore = environment.router["Views"].getActiveMarkets(currencyId)
    # TODO: need to add some trading in or this will net off to zero

    environment.router["RedeemPerpetual"].perpetualTokenRedeem(
        currencyId, 1e8, True, {"from": accounts[0]}
    )
    perp_token_asserts(environment, currencyId, False, accounts, wasInit=False)

    marketsAfter = environment.router["Views"].getActiveMarkets(currencyId)
    (cashBalanceAfter, perpTokenBalanceAfter, lastMintTimeAfter) = environment.router[
        "Views"
    ].getAccountBalance(currencyId, accounts[0])

    # Assert that no assets in portfolio
    assert len(environment.router["Views"].getAccountPortfolio(accounts[0])) == 0

    # assert decrease in market liquidity
    assert len(marketsBefore) == len(marketsAfter)
    for (i, m) in enumerate(marketsBefore):
        assert m[4] > marketsAfter[i][4]

    assert cashBalanceBefore > cashBalanceAfter
    assert perpTokenBalanceAfter == perpTokenBalanceBefore - 100e8
    assert lastMintTimeAfter > lastMintTimeBefore


@pytest.mark.skip
def test_redeem_and_put_into_portfolio(environment, accounts):
    initialize_markets(environment, accounts)
    currencyId = 2

    (cashBalanceBefore, perpTokenBalanceBefore, lastMintTimeBefore) = environment.router[
        "Views"
    ].getAccountBalance(currencyId, accounts[0])
    marketsBefore = environment.router["Views"].getActiveMarkets(currencyId)

    # TODO: need to add some trading in or this will net off to zero

    environment.router["RedeemPerpetual"].perpetualTokenRedeem(
        currencyId, 100e8, False, {"from": accounts[0]}
    )
    perp_token_asserts(environment, currencyId, False, accounts, wasInit=False)

    marketsAfter = environment.router["Views"].getActiveMarkets(currencyId)
    (cashBalanceAfter, perpTokenBalanceAfter, lastMintTimeAfter) = environment.router[
        "Views"
    ].getAccountBalance(currencyId, accounts[0])

    portfolio = environment.router["Views"].getAccountPortfolio(accounts[0])
    assert len(portfolio) == 2

    # Assert that assets in portfolio
    # assert decrease in market liquidity
    assert len(marketsBefore) == len(marketsAfter)
    for (i, m) in enumerate(marketsBefore):
        assert m[4] > marketsAfter[i][4]

    assert cashBalanceBefore == cashBalanceAfter
    assert perpTokenBalanceAfter == perpTokenBalanceBefore - 100e8
    assert lastMintTimeAfter > lastMintTimeBefore


def test_redeem_all_liquidity_and_initialize(environment, accounts):
    initialize_markets(environment, accounts)
    currencyId = 2

    environment.router["RedeemPerpetual"].perpetualTokenRedeem(
        currencyId, INITIAL_CASH_AMOUNT, True, {"from": accounts[0]}
    )

    perpTokenAddress = environment.router["Views"].getPerpetualTokenAddress(currencyId)
    portfolio = environment.router["Views"].getAccountPortfolio(perpTokenAddress)
    ifCashAssets = environment.router["Views"].getifCashAssets(perpTokenAddress)

    # assert no assets in perp token
    assert len(portfolio) == 0
    assert len(ifCashAssets) == 0

    environment.router["MintPerpetual"].perpetualTokenMint(
        currencyId, INITIAL_CASH_AMOUNT, False, {"from": accounts[0]}
    )

    # Set is first init to true if the market does not have assets
    environment.router["InitializeMarkets"].initializeMarkets(currencyId, True)
    perp_token_asserts(environment, currencyId, True, accounts)


@pytest.mark.skip
def test_mint_above_leverage_threshold(environment, accounts):
    initialize_markets(environment, accounts)
    currencyId = 2

    environment.router["Governance"].updatePerpetualDepositParameters(
        currencyId, [0.4e8, 0.4e8], [0.4e9, 0.4e9]
    )

    perpTokenAddress = environment.router["Views"].getPerpetualTokenAddress(currencyId)
    portfolioBefore = environment.router["Views"].getAccountPortfolio(perpTokenAddress)
    ifCashAssetsBefore = environment.router["Views"].getifCashAssets(perpTokenAddress)

    environment.router["MintPerpetual"].perpetualTokenMint(
        currencyId, 100e8, False, {"from": accounts[0]}
    )

    portfolioAfter = environment.router["Views"].getAccountPortfolio(perpTokenAddress)
    ifCashAssetsAfter = environment.router["Views"].getifCashAssets(perpTokenAddress)

    # No liquidity tokens added
    assert portfolioBefore == portfolioAfter

    # fCash amounts have increased in the portfolio
    for (i, asset) in enumerate(ifCashAssetsBefore):
        assert asset[3] < ifCashAssetsAfter[i][3]

    blockTime = chain.time()
    chain.mine(1, timestamp=(blockTime + SECONDS_IN_QUARTER))

    environment.router["Governance"].updatePerpetualDepositParameters(
        currencyId, [0.4e8, 0.4e8], [0.8e9, 0.8e9]
    )

    environment.router["InitializeMarkets"].initializeMarkets(currencyId, False)
    perp_token_asserts(environment, currencyId, False, accounts)


# def test_settle_and_negative_fcash(environment, accounts):
#     pass
