"""Private forward evidence qualification; no transport or financial acceptance."""
import hashlib
import json
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from application.flex_daily.checks import number, require_account_scope

FORWARD_START = '2026-11-03'
# Cash feeds overlap: retain each section as a separate observation, never sum.
EVENTS = {
    'CashTransactions': 'transactionID',
    'StmtFunds': 'transactionID',
    'Trades': 'tradeID',
    'Transfers': 'transactionID',
    'TradeTransfers': 'tradeID',
    'CorporateActions': 'transactionID',
    'UnsettledTransfers': 'transactionID',
    'OpenDividendAccruals': 'transactionID',
    'ChangeInDividendAccruals': 'transactionID',
}
QUANTITY = {'Trades', 'Transfers', 'TradeTransfers', 'CorporateActions'}


def instant(value, zone=None):
    """Require an explicit offset; date-only and naive native facts stay unknown."""
    if not isinstance(value, str) or 'T' not in value:
        raise ValueError('time_unqualified')
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('time_unqualified')
    if zone is not None and parsed.utcoffset() != parsed.astimezone(zone).utcoffset():
        raise ValueError('offset_zone_mismatch')
    return parsed.astimezone(timezone.utc)


def validate_config(config):
    if config.get('forward_start') != FORWARD_START:
        raise ValueError('forward_start_invalid')
    # Generation/collection timestamps can never be configured as financial as-of.
    if config.get('snapshot_time_field') in {'whenGenerated', 'fromDate', 'toDate'}:
        raise ValueError('snapshot_field_not_financial_asof')


def qualify(content, config, checked, previous, capture_start, capture_end):
    """Use only mapped native snapshot fields, never infer from report dates.

    `previous` contains only prior in-scope private projections, not old raw XML.
    The configured field names need independent source qualification before use.
    """
    root = require_account_scope(content, config['accounts'])
    result = {}
    boundary = datetime.combine(datetime.fromisoformat(FORWARD_START).date(), time(),
                                ZoneInfo('America/New_York')).astimezone(timezone.utc)
    for statement in root.findall('./FlexStatements/FlexStatement'):
        label = config['accounts'][statement.get('accountId')]
        prior = previous.get(label) or {}
        if prior.get('account_id') != statement.get('accountId'):
            prior = {}
        issues = []
        ev = {'account_id': statement.get('accountId'),
              'source_sha256': hashlib.sha256(content).hexdigest(),
              'previous_source_sha256': prior.get('source_sha256'),
              'source_as_of': None, 'previous_actual_cutoff': None,
              'capture_started_at': capture_start.isoformat(), 'capture_finished_at': capture_end.isoformat(),
              'anchor_qualified': False, 'events': [], 'cash': [], 'positions': [],
              'seen_events': dict(prior.get('seen_events', {})), 'issues': issues,
              'reconciliation_status': 'UNRECONCILED'}
        cutoff = None
        try:
            zone_name = statement.get(config.get('snapshot_timezone_field', ''))
            zone = ZoneInfo(zone_name)
            native = statement.get(config.get('snapshot_time_field', ''))
            cutoff = instant(native, zone)
            if not boundary <= cutoff <= capture_end or capture_start > capture_end:
                raise ValueError('snapshot_outside_forward_capture')
            if cutoff.astimezone(zone).date().isoformat() != datetime.strptime(statement.get('toDate', '').replace('-', ''), '%Y%m%d').date().isoformat():
                raise ValueError('snapshot_report_day_mismatch')
            ev['source_as_of'] = {'native': native, 'timezone': zone_name,
                                  'utc': cutoff.isoformat(), 'offset': str(datetime.fromisoformat(native).utcoffset())}
        except (ValueError, TypeError, ZoneInfoNotFoundError):
            issues.append('snapshot_time_unqualified')
            cutoff = None
        structural = checked[label]['status'] == 'complete' and checked[label]['reconciliation_status'] != 'difference'
        for row in statement.findall('./CashReport/CashReportCurrency'):
            if row.get('currency') == 'BASE_SUMMARY':
                continue
            if row.get('endingSettledCash') is not None:
                try:
                    number(row.attrib, 'endingSettledCash')
                except ValueError:
                    issues.append('settled_cash_unqualified')
            ev['cash'].append({'currency': row.get('currency'), 'definition': 'endingCash',
                               'closing': row.get('endingCash'), 'settled': row.get('endingSettledCash'),
                               'settled_definition': 'endingSettledCash' if row.get('endingSettledCash') is not None else None})
        for row in statement.findall('./OpenPositions/OpenPosition'):
            ev['positions'].append(dict(row.attrib))
            if row.get('assetCategory') != 'STK':
                issues.append('instrument_coverage_unqualified')
        ev['anchor_qualified'] = cutoff is not None and structural and not issues
        lower = None
        if prior.get('anchor_qualified'):
            try:
                lower = instant(prior['source_as_of']['utc'])
                if lower < boundary or cutoff is None or lower >= cutoff:
                    raise ValueError('anchor_order')
                ev['previous_actual_cutoff'] = lower.isoformat()
            except (KeyError, TypeError, ValueError):
                lower = None
                issues.append('previous_anchor_unqualified')
        if lower is None:
            issues.append('opening_snapshot_missing')
        seen_in_report = {}
        for section, identity_field in EVENTS.items():
            for row in statement.findall('./' + section + '/*'):
                facts = dict(row.attrib)
                event = {'section': section, 'facts': facts, 'eligibility': 'unknown', 'effective_utc': None}
                ev['events'].append(event)
                identity = row.get(identity_field)
                key = section + ':' + (identity or '')
                fingerprint = hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()
                if not identity or key in seen_in_report:
                    if key in seen_in_report:
                        seen_in_report[key]['eligibility'] = 'unknown'
                    issues.append('event_identity_duplicate' if identity else 'event_identity_missing')
                    continue
                seen_in_report[key] = event
                if any(row.get(k) for k in ('originalTradeID', 'originalTransactionID', 'origTradeID', 'origTransactionID')):
                    issues.append('correction_linkage_unqualified')
                    continue
                if not row.get('currency', '').strip() or row.get('currency') == 'BASE_SUMMARY':
                    issues.append('event_currency_unqualified')
                    continue
                if section in QUANTITY and (not row.get('conid') or row.get('assetCategory') != 'STK'):
                    issues.append('instrument_coverage_unqualified')
                    continue
                try:
                    time_field = config.get('event_time_fields', {}).get(section)
                    if not time_field or time_field in {'whenGenerated', 'reportDate'}:
                        raise ValueError('event_time_semantics_unqualified')
                    number(facts, 'quantity' if section in QUANTITY else 'amount')
                    effective = instant(row.get(time_field))
                    event['effective_utc'] = effective.isoformat()
                except ValueError:
                    issues.append('event_effective_time_or_amount_unqualified')
                    continue
                known = ev['seen_events'].get(key)
                if known is not None and known != fingerprint:
                    issues.append('event_revision_unqualified')
                    continue
                if effective < boundary:
                    event['eligibility'] = 'excluded_pre_start'
                elif cutoff is None or lower is None:
                    issues.append('event_interval_unqualified')
                elif effective <= lower:
                    event['eligibility'] = 'excluded_before_or_at_previous'
                elif effective > cutoff:
                    event['eligibility'] = 'excluded_after_current'
                elif known is not None:
                    event['eligibility'] = 'duplicate_observation'
                else:
                    event['eligibility'] = 'selected'
                    ev['seen_events'][key] = fingerprint
        ev['issues'] = sorted(set(issues))
        result[label] = ev
    return result
