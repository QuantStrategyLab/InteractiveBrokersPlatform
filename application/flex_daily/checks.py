"""Versioned structural checks and exact Decimal residuals, never adjustments."""
from decimal import Decimal, InvalidOperation, localcontext
from datetime import datetime, timedelta
from collections import Counter
from xml.etree import ElementTree as ET

from application.ibkr_flex_source import _xml_root

SCHEMA = 1
# Empty activity containers are meaningful only after query configuration has
# independently confirmed these sections. Balance containers require rows.
FIELDS = {
    'OpenPositions': ('OpenPosition', 'conid currency position levelOfDetail reportDate'),
    'Trades': ('Trade', 'conid currency quantity tradeID originalTradeID levelOfDetail reportDate'),
    'Transfers': ('Transfer', 'conid currency quantity transactionID direction date'),
    'TradeTransfers': ('TradeTransfer', 'conid currency quantity tradeID originalTradeID direction'),
    'UnsettledTransfers': ('UnsettledTransfer', 'conid currency quantity transactionID stage'),
    'CorporateActions': ('CorporateAction', 'conid currency quantity transactionID reportDate'),
    'CashReport': ('CashReportCurrency', 'currency startingCash endingCash fromDate toDate'),
    'CashTransactions': ('CashTransaction', 'currency amount type dateTime transactionID'),
    'StmtFunds': ('StatementOfFundsLine', 'currency amount activityCode date transactionID'),
    'InterestAccruals': ('InterestAccrualsCurrency', 'currency fromDate toDate startingAccrualBalance interestAccrual accrualReversal endingAccrualBalance'),
    'OpenDividendAccruals': ('OpenDividendAccrual', 'conid currency quantity grossAmount netAmount tax fee'),
    'ChangeInDividendAccruals': ('ChangeInDividendAccrual', 'conid currency quantity grossAmount netAmount tax fee code'),
    'EquitySummaryInBase': ('EquitySummaryByReportDateInBase', 'currency reportDate total'),
    'ConversionRates': ('ConversionRate', 'fromCurrency toCurrency rate reportDate'),
}
NUMBERS = {'position', 'quantity', 'amount', 'startingCash', 'endingCash', 'startingAccrualBalance',
           'interestAccrual', 'accrualReversal', 'endingAccrualBalance', 'grossAmount', 'netAmount',
           'tax', 'fee', 'total', 'rate'}


def number(row, field):
    try:
        value = Decimal(row[field])
    except (KeyError, InvalidOperation, TypeError):
        raise ValueError('numeric_field_missing_or_invalid') from None
    if not value.is_finite():
        raise ValueError('numeric_field_nonfinite')
    return value


def inspect_report(content, start, end, accounts, configured_sections, previous_content=None):
    root = _xml_root(content, endpoint='GetStatement')
    if root.tag != 'FlexQueryResponse' or root.get('type') != 'AF':
        raise ValueError('not_activity_report')
    statements = root.findall('./FlexStatements/FlexStatement')
    ids = [s.get('accountId') for s in statements]
    if not ids or len(ids) != len(set(ids)) or not set(ids) <= set(accounts):
        raise ValueError('account_scope_mismatch')
    results = {label: {'status': 'missing', 'issues': ['account_missing'], 'reconciliation_status': 'partial'}
               for label in accounts.values()}
    for statement in statements:
        account = statement.get('accountId')
        if (statement.get('fromDate', '').replace('-', '') != start.replace('-', '')
                or statement.get('toDate', '').replace('-', '') != end.replace('-', '')
                or statement.get('model', '') or not statement.get('whenGenerated')):
            raise ValueError('period_model_or_generation_missing')
        if any(row.get('accountId') not in (None, '', account) or row.get('model', '') for row in statement.iter()):
            raise ValueError('nested_scope_mismatch')
        issues = []
        try:
            datetime.strptime(statement.get('whenGenerated'), '%Y%m%d;%H%M%S')
        except (TypeError, ValueError):
            issues.append('generation_format_unqualified')
        for section, (tag, fields) in FIELDS.items():
            containers = statement.findall(section)
            if len(containers) != 1 or section not in configured_sections:
                issues.append('section_missing_or_unconfigured:' + section)
                continue
            rows = list(containers[0])
            if not rows and section in {'CashReport', 'EquitySummaryInBase', 'InterestAccruals', 'ConversionRates'}:
                issues.append('balance_rows_missing:' + section)
            seen = set()
            for row in rows:
                if row.tag != tag or len(row) or not set(fields.split()) <= set(row.attrib):
                    issues.append('field_or_shape_missing:' + section)
                    continue
                for field in ('reportDate', 'fromDate', 'toDate'):
                    value = row.get(field)
                    if value is not None:
                        try:
                            parsed = datetime.strptime(value, '%Y%m%d').date().isoformat()
                            if not start <= parsed <= end:
                                issues.append('row_period_mismatch:' + section)
                        except ValueError:
                            issues.append('row_date_invalid:' + section)
                if section == 'OpenPositions' and row.get('reportDate', '').replace('-', '') != end.replace('-', ''):
                    issues.append('position_snapshot_date_mismatch')
                try:
                    for field in set(fields.split()) & NUMBERS:
                        number(row.attrib, field)
                except ValueError:
                    issues.append('numeric_invalid:' + section)
                if section == 'OpenPositions' and row.get('levelOfDetail') != 'SUMMARY':
                    issues.append('position_level_invalid')
                if section == 'Trades' and (row.get('levelOfDetail') != 'EXECUTION' or row.get('originalTradeID')):
                    issues.append('trade_level_or_correction_requires_review')
                # Do not collapse equal date/symbol/amount rows. Explicit duplicate
                # identities are ambiguous and require review, never double count.
                identity = next((row.get(k) for k in ('tradeID', 'transactionID') if row.get(k)), None)
                if identity and identity in seen:
                    issues.append('duplicate_identity:' + section)
                if identity:
                    seen.add(identity)
        nav = statement.find('ChangeInNAV')
        if nav is None or 'ChangeInNAV' not in configured_sections:
            issues.append('section_missing_or_unconfigured:ChangeInNAV')
        residuals = {}
        try:
            with localcontext() as context:
                context.prec = 50
                for index, row in enumerate(statement.findall('./CashReport/CashReportCurrency')):
                    residuals[f'cash_{index}'] = str(number(row.attrib, 'endingCash') - number(row.attrib, 'startingCash')
                                                   - sum((number(row.attrib, k) for k in CASH_FIELDS), Decimal(0)))
                funds = statement.find('StmtFunds')
                if funds is not None and 'StmtFunds' in configured_sections:
                    for index, row in enumerate(statement.findall('./CashReport/CashReportCurrency')):
                        currency = row.get('currency')
                        if currency != 'BASE_SUMMARY':
                            residuals[f'cash_funds_{index}'] = str(number(row.attrib, 'endingCash') - number(row.attrib, 'startingCash')
                                - sum((number(r.attrib, 'amount') for r in funds if r.get('currency') == currency), Decimal(0)))
                if nav is not None:
                    # These overlapping subtotals are excluded only with explicit
                    # zero evidence, matching the existing local verifier.
                    if any(number(nav.attrib, k) != 0 for k in ('realized', 'changeInUnrealized', 'forexCommissions')):
                        raise ValueError('nav_overlap_unqualified')
                    known = set(NAV_FIELDS) | {'accountId', 'model', 'currency', 'fromDate', 'toDate', 'startingValue',
                                             'endingValue', 'twr', 'realized', 'changeInUnrealized', 'forexCommissions'}
                    if set(nav.attrib) - known:
                        raise ValueError('nav_unknown_component')
                    residuals['nav'] = str(number(nav.attrib, 'endingValue') - number(nav.attrib, 'startingValue')
                                           - sum((number(nav.attrib, k) for k in NAV_FIELDS), Decimal(0)))
        except ValueError:
            issues.append('reconciliation_fields_unqualified')
        if previous_content:
            previous_root = _xml_root(previous_content, endpoint='GetStatement')
            previous_day = (datetime.fromisoformat(start).date() - timedelta(days=1)).strftime('%Y%m%d')
            previous = next((s for s in previous_root.findall('./FlexStatements/FlexStatement')
                             if s.get('accountId') == account and s.get('toDate', '').replace('-', '') == previous_day), None)
            if previous is not None:
                try:
                    residuals.update(opening_bridges(previous, statement))
                except ValueError:
                    issues.append('opening_bridge_unqualified')
        # Grant, independent opening snapshots, settlement and accrual lineage
        # remain distinct from automatic schema completeness. Never report matched.
        results[accounts[account]] = {
            'status': 'received' if issues else 'complete', 'issues': sorted(set(issues)),
            'residuals': residuals,
            'reconciliation_status': 'difference' if any(Decimal(x) != 0 for x in residuals.values()) else 'partial',
            'manual_evidence_required': ['grant_activity', 'independent_opening', 'settlement_and_accrual_lineage'],
        }
    return results


def opening_bridges(previous, current):
    """Reuse exact opening/closing arithmetic; grants remain a coverage limit."""
    def balances(statement, section, tag, key, amount):
        container = statement.find(section)
        if container is None:
            raise ValueError('opening_section_missing')
        result = {}
        for row in container.findall(tag):
            identity = row.get(key)
            if not identity or identity in result:
                raise ValueError('ambiguous_balance')
            if section == 'OpenPositions' and row.get('levelOfDetail') != 'SUMMARY':
                raise ValueError('position_level')
            result[identity] = number(row.attrib, amount)
        return result
    residuals = {}
    with localcontext() as context:
        context.prec = 50
        opening_cash = balances(previous, 'CashReport', 'CashReportCurrency', 'currency', 'endingCash')
        for i, row in enumerate(current.findall('./CashReport/CashReportCurrency')):
            if row.get('currency') not in opening_cash:
                raise ValueError('opening_currency_missing')
            residuals[f'cash_opening_{i}'] = str(number(row.attrib, 'startingCash') - opening_cash[row.get('currency')])
        before = balances(previous, 'OpenPositions', 'OpenPosition', 'conid', 'position')
        after = balances(current, 'OpenPositions', 'OpenPosition', 'conid', 'position')
        movements = {}
        seen = set()
        for section, tag, identity_field in (('Trades', 'Trade', 'tradeID'), ('Transfers', 'Transfer', 'transactionID'),
                                            ('TradeTransfers', 'TradeTransfer', 'tradeID'), ('CorporateActions', 'CorporateAction', 'transactionID')):
            container = current.find(section)
            if container is None:
                raise ValueError('quantity_activity_missing')
            for row in container.findall(tag):
                if row.get('assetCategory') == 'CASH':
                    continue
                identity = row.get(identity_field)
                # Cross-section trade IDs share a namespace. Ambiguous duplicate
                # or correction records are reviewed, never added twice.
                key = (identity_field, identity)
                if not identity or key in seen or row.get('originalTradeID'):
                    raise ValueError('quantity_event_identity_unqualified')
                seen.add(key)
                if section == 'Trades' and row.get('levelOfDetail') != 'EXECUTION':
                    raise ValueError('trade_level')
                conid = row.get('conid')
                if not conid:
                    raise ValueError('quantity_conid_missing')
                movements[conid] = movements.get(conid, Decimal(0)) + number(row.attrib, 'quantity')
        for i, conid in enumerate(sorted(before.keys() | after.keys() | movements.keys())):
            # Explicit empty snapshots and activity containers are required by
            # inspect_report before this can contribute to a complete date.
            residuals[f'quantity_observed_{i}'] = str(after.get(conid, Decimal(0)) - before.get(conid, Decimal(0)) - movements.get(conid, Decimal(0)))
        old_nav, new_nav = previous.find('ChangeInNAV'), current.find('ChangeInNAV')
        if old_nav is not None and new_nav is not None:
            residuals['nav_opening'] = str(number(new_nav.attrib, 'startingValue') - number(old_nav.attrib, 'endingValue'))
    return residuals


def compare_activity(monthly, daily_contents):
    """Compare activity multisets, preserving true identical rows and row counts.

    Metadata varying by report generation is excluded; daily balance snapshots
    are not aggregated into monthly activity. Missing days are checked by caller.
    """
    sections = ('Trades', 'Transfers', 'TradeTransfers', 'CorporateActions', 'CashTransactions', 'StmtFunds', 'ChangeInDividendAccruals')
    def rows(payloads):
        result = Counter()
        for payload in payloads:
            root = _xml_root(payload, endpoint='GetStatement')
            for statement in root.findall('./FlexStatements/FlexStatement'):
                for section in sections:
                    for container in statement.findall(section):
                        for row in container:
                            attrs = tuple(sorted((k, v) for k, v in row.attrib.items()
                                                 if k not in {'whenGenerated', 'fromDate', 'toDate'}))
                            result[(statement.get('accountId'), section, row.tag, attrs)] += 1
        return result
    return rows([monthly]) == rows(daily_contents)


def private_projection(content):
    """Preserve source fields and row order. No invented balances or dedup sums."""
    root = ET.fromstring(content)
    return [{'tag': node.tag, 'attributes': node.attrib} for node in root.iter()]


CASH_FIELDS = '''clientFees commissions billableCommissions depositWithdrawals
accountTransfers linkingAdjustments internalTransfers debitCardActivity billPay
dividends brokerInterest bondInterest cashSettlingMtm netTradesSales netTradesPurchases
otherFees advisorFees feesReceivables paymentInLieu transactionTax taxReceivables
withholdingTax withholding871m fxTranslationGainLoss salesTax billableSalesTax
brokerFees carbonCredits cfdCharges commissionCreditsRedemption donations excessFundSweep
insuredDepositInterest ipoSubscription other otherIncome paxosTransfers realizedForexVm
realizedVm referralFee withholdingCollectedTax slbNetSecuritiesLentActivity'''.split()
NAV_FIELDS = '''mtm depositsWithdrawals internalCashTransfers assetTransfers dividends
withholdingTax withholding871m changeInDividendAccruals interest changeInInterestAccruals
advisorFees clientFees otherFees feesReceivables commissions commissionReceivables
transactionTax taxReceivables salesTax softDollars netFxTrading fxTranslation linkingAdjustments
other billPay billableSalesTax brokerFees carbonCredits changeInBrokerFeeAccruals
changeInCGTWithholdingAccruals changeInIncentiveCouponAccruals changeInLiteSurchargeAccruals
commissionCreditsRedemption commissionsAtPaxos corporateActionProceeds costAdjustments
debitCardActivity donations excessFundSweep grantActivity mtmAtPaxos otherIncome paxosTransfers
referralFee transferredPnlAdjustments withholdingTaxCollected'''.split()
