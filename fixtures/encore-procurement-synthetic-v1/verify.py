
"""Independent arithmetic/fixture checks. No third-party packages required."""
import hashlib, json, tempfile
from datetime import datetime, date, timedelta
from generate import generate

a=generate();b=generate()
assert a==b,'Generation must be byte-identical'
def load(n):return json.loads(a[n])
manifest=load('manifest.json')
for path,h in manifest['files'].items():assert hashlib.sha256(a[path].encode()).hexdigest()==h
key=load('evaluation/answer_key.json')
quotes=load('data/quotes.json');hist=load('data/historical_contracts.json')
cash=load('data/cash_inputs.json')
for q in quotes[:3]:
    expected=key['procurement_as_of_2026_10_02'][q['id']]
    total=None if q['delivery_cents'] is None else q['base_cents']+q['delivery_cents']+q['tax_cents']
    assert total==expected['comparable_total_cents']
    if total is None:continue
    events={}
    def event(d,amt):events[d]=events.get(d,0)+amt
    for x in cash['scheduled_outflows']:event(x['date'],-x['amount_cents'])
    for x in cash['expected_receipts']:event(x['date'],x['amount_cents'])
    event(q['deposit_due'],-q['deposit_cents']);event(q['balance_due'],-(total-q['deposit_cents']))
    closing=cash['opening_posted_cents'];first=None;minimum=closing
    d=date(2026,10,2)
    while d<=date(2026,11,1):
        closing+=events.get(d.isoformat(),0)
        if d<date(2026,10,8):minimum=min(minimum,closing)
        if closing<cash['buffer_cents'] and first is None:first=d.isoformat()
        d+=timedelta(days=1)
    assert first==expected['first_buffer_breach_date']
    assert minimum==expected['minimum_closing_cents_before_receipt']
for h in hist:
    k=key['history'][h['id']];net=h['final_invoiced_cents']-h['credits_cents']
    assert net==k['net_cost_cents']
    assert net-h['original_budget_cents']==k['variance_vs_original_cents']
    assert net-h['original_budget_cents']-h['approved_scope_changes_cents']==k['variance_vs_revised_cents']
r=load('data/revenue_history.json')[0];k=key['revenue']
projected=r['original_projected_gross_cents']*r['festival_share_bps']//10000
actual=r['actual_gross_cents']*r['festival_share_bps']//10000
assert projected==k['projected_festival_share_cents'] and actual==k['actual_festival_share_cents']
assert actual-r['cash_collected_cents']==k['receivable_cents']
obs=load('data/sla_observations.json')
for o in obs:
    inc=o['incident'];parse=datetime.fromisoformat
    assert (parse(inc['acknowledged_at'])-parse(inc['opened_at'])).total_seconds()/60==18
    assert (parse(inc['restored_at'])-parse(inc['opened_at'])).total_seconds()/60==50
    if o['id']=='late_supplier':
        cutoff=parse('2026-10-15T12:30:00-07:00')
        delivered=sum(x['qty'] for x in o['deliveries'] if parse(x['available_at'])<=cutoff)
        assert delivered==4
        assert delivered*375000-800000==700000
        deadline=parse('2026-10-15T12:00:00-07:00');last=max(parse(x['accepted_at']) for x in o['deliveries'])
        hours=(int((last-deadline).total_seconds())+3599)//3600
        assert min(hours*50000,200000)==100000
    if o['id']=='missing_receipt':assert not o['deliveries']
obligations=load('data/obligations.json');payments=load('data/payments.json')
assert sum(o['amount_cents'] for o in obligations)==2250000
assert sum(o['amount_cents'] for o in obligations)-sum(p['amount_cents'] for p in payments)==1450000
ids=[s['id'] for s in load('data/source_index.json')];assert len(ids)==len(set(ids))
for q in quotes:assert q['source_document_id'] in ids
for o in obs:
    for receipt in o['deliveries']:assert receipt['source_document_id'] in ids
print('PASS: deterministic generation, hashes, quote costs, daily cash, history, revenue, timing, SLA arithmetic, obligations and source references.')
print('These validate fixtures, not the Encore application. App approval, access, deduplication and payment tests remain required.')
