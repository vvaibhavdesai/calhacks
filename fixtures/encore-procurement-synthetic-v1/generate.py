
"""Fictional Encore procurement fixtures. Python 3.11+, stdlib only.
Run: python generate.py --output ./generated --seed 20261024
Never use the evaluation directory as agent evidence.
"""
import argparse, csv, hashlib, io, json, random
from pathlib import Path

def generate(seed=20261024):
    rng=random.Random(seed)
    files={}
    def put(path,obj):
        files[path]=json.dumps(obj,ensure_ascii=False,sort_keys=True,indent=2)+'\n'
    docs=[]
    def doc(id,title,text,available):
        path='sources/'+id+'.md'
        files[path]='# '+title+'\n\nFICTIONAL SYNTHETIC DEMO DATA. Not a real supplier, agreement, signature or legal remedy.\n\n'+text.strip()+'\n'
        docs.append({'id':id,'path':path,'title':title,'available_at':available})
        return id
    festival={'id':'fest_demo_2026','name':'Encore Demo Festival','synthetic':True,'timezone':'America/Los_Angeles',
      'event_start':'2026-10-16T12:00:00-07:00','event_end':'2026-10-18T23:00:00-07:00',
      'window_start':'2026-09-21','window_end':'2026-11-01','currency':'USD',
      'procurement_as_of':'2026-10-02T09:00:00-07:00',
      'integration_note':'Proposed fixture dates and IDs. Map to the existing repo festival/calendar; do not create another festival or overwrite its 80 vendors.'}
    put('data/festival.json',festival)
    suppliers=[
      {'id':'sup_a','name':'Copperline Power (fictional)','category':'generator_rental','relationship':'returning','revenue_applicable':False},
      {'id':'sup_b','name':'ClearPeak Energy (fictional)','category':'generator_rental','relationship':'new','revenue_applicable':False},
      {'id':'sup_c','name':'Harbor Current (fictional)','category':'generator_rental','relationship':'returning','revenue_applicable':False},
      {'id':'partner_d','name':'Meadow Refreshments (fictional)','category':'beverage_concession','relationship':'returning','revenue_applicable':True,
       'note':'Separate revenue-generating partner example, never ranked against generator suppliers.'}]
    put('data/counterparties.json',suppliers)
    req={'id':'req_gen_2026','festival_id':festival['id'],'category':'generator_rental','quantity':6,'item_code':'GEN100',
      'specification':'100 kVA diesel generator, 120/208 V three-phase; six equivalent units',
      'rental_start':'2026-10-15','rental_end':'2026-10-19','delivery_deadline':'2026-10-15T12:00:00-07:00',
      'installation_deadline':'2026-10-16T10:00:00-07:00','requirements':[
       {'code':'QTY_SPEC','mandatory':True,'description':'Six generators at the stated specification'},
       {'code':'DELIVERY','mandatory':True,'description':'Delivery and collection pricing stated'},
       {'code':'INSTALL','mandatory':True,'description':'Installation and load testing included'},
       {'code':'SUPPORT','mandatory':True,'description':'On-call support during event hours'},
       {'code':'INSURANCE','mandatory':True,'description':'Insurance certificate supplied before commitment'}],
      'tax_policy':'Synthetic 0% for this fixture only; not a claim about California tax law.',
      'currency':'USD','service_location':'Fictional Meadow Park site'}
    doc('req_gen_2026','Generator procurement requirements',
       'Clause R1: Six GEN100 units, 100 kVA each, for October 15 through October 19, 2026.\n\nClause R2: Delivery by October 15 at 12:00 America/Los_Angeles. Installed and load-tested by October 16 at 10:00.\n\nClause R3: Quotes must state delivery/collection cost, installation, event-hours on-call support and insurance evidence. Demo tax is explicitly 0%.', '2026-09-25T09:00:00-07:00')
    req['source_document_id']='req_gen_2026';put('data/procurement_requests.json',[req])
    quote_specs=[('q_a_v1','sup_a',1800000,500000,500000,'2026-10-05','2026-10-12',True),
                 ('q_b_v1','sup_b',2100000,0,1200000,'2026-10-05','2026-10-20',True),
                 ('q_c_v1','sup_c',1900000,None,400000,'2026-10-05','2026-10-20',False)]
    quotes=[]
    for qid,sid,base,delivery,deposit,due,finaldue,insurance in quote_specs:
        gross=None if delivery is None else base+delivery
        txt=f'Clause Q1: Request req_gen_2026. Six GEN100 units at the requested specification. Base rental USD {base/100:,.2f}.\n\n'
        txt+='Clause Q2: Delivery and collection '+('not yet priced; a separate charge will be confirmed.' if delivery is None else ('included at USD 0.00.' if delivery==0 else f'USD {delivery/100:,.2f}, mandatory.'))
        txt+=f'\n\nClause Q3: Installation, load testing and event-hours on-call support included. Deposit USD {deposit/100:,.2f} due {due}; remaining agreed balance due {finaldue}. Deposit non-refundable after booking. Quote valid through October 7, 2026.'
        txt+='\n\nClause Q4: '+('Insurance certificate attached as schedule I.' if insurance else 'Insurance certificate pending.')
        txt+='\n\nClause Q5: Synthetic sales tax 0%. No other mandatory charges stated. Receipt of a quote does not form a contract.'
        did=doc(qid,'Generator quote '+qid,txt,'2026-10-01T09:00:00-07:00')
        quotes.append({'id':qid,'version':1,'supplier_id':sid,'request_id':req['id'],'currency':'USD','source_document_id':did,
          'available_at':'2026-10-01T09:00:00-07:00','valid_until':'2026-10-07','quantity':6,'base_cents':base,'delivery_cents':delivery,
          'tax_cents':0,'deposit_cents':deposit,'deposit_due':due,'balance_due':finaldue,'deposit_refundable':False,
          'installation_included':True,'support_included':True,'insurance_evidence':insurance,
          'citations':{'price':'Q1','delivery':'Q2','payment':'Q3','insurance':'Q4','tax':'Q5'}})
    # Future evidence must never leak into an October 2 recommendation.
    doc('q_b_v2','Revised ClearPeak quote',
       'Clause Q1: Supersedes q_b_v1. Same scope, total USD 22,500 all inclusive.\n\nClause Q2: Deposit USD 8,000 due October 7; balance USD 14,500 due October 20.\n\nClause Q3: Available October 6, 09:00 America/Los_Angeles. Valid through October 8. Internal selection of v1 must be reviewed again before committing to this revision.', '2026-10-06T09:00:00-07:00')
    revised=dict(quotes[1]);revised.update(id='q_b_v2',version=2,source_document_id='q_b_v2',base_cents=2250000,deposit_cents=800000,deposit_due='2026-10-07',
      valid_until='2026-10-08',available_at='2026-10-06T09:00:00-07:00',supersedes='q_b_v1',citations={'price':'Q1','payment':'Q2'})
    put('data/quotes.json',quotes+[revised])
    history=[];hist_lines=[];contracts=[]
    historical=[('hist_a_2025','sup_a',2000000,300000,2700000,100000,2600000),
                ('hist_c_2025','sup_c',1900000,50000,1950000,0,1950000)]
    for hid,sid,original,changes,invoices,credits,paid in historical:
        did=doc(hid,'Historical generator contract and close '+hid,
          f'Clause H1: 2025 fictional three-day event. Six GEN100 units for five rental days, including delivery, installation and on-call support.\n\nClause H2: Original approved budget USD {original/100:,.2f}. Approved scope changes USD {changes/100:,.2f}.\n\nClause H3: Final invoiced USD {invoices/100:,.2f}, issued credits USD {credits/100:,.2f}; final net cost USD {(invoices-credits)/100:,.2f}; cash paid USD {paid/100:,.2f}. Close finalized September 1, 2026.\n\nClause H4: Differences may include agreed scope changes; historical overrun alone does not establish supplier fault.', '2026-09-01T09:00:00-07:00')
        history.append({'id':hid,'supplier_id':sid,'period':'2025','status':'final','source_document_id':did,
          'scope':{'item_code':'GEN100','quantity':6,'rental_days':5},'original_budget_cents':original,
          'approved_scope_changes_cents':changes,'final_invoiced_cents':invoices,'credits_cents':credits,
          'final_net_cost_cents':invoices-credits,'cash_paid_cents':paid,'projected_revenue_cents':None,'actual_revenue_cents':None})
        hist_lines += [{'id':hid+'_inv','contract_id':hid,'kind':'invoice','amount_cents':invoices,'source_document_id':did,'clause':'H3'},
          {'id':hid+'_pay','contract_id':hid,'kind':'payment','amount_cents':paid,'source_document_id':did,'clause':'H3'}]
        if credits:hist_lines.append({'id':hid+'_credit','contract_id':hid,'kind':'credit','amount_cents':credits,'source_document_id':did,'clause':'H3'})
    doc('hist_concession_2025','Historical beverage concession agreement and close',
      'Clause V1: Meadow Refreshments operated a fictional nonalcoholic beverage concession. Original projected gross revenue USD 200,000; festival share 15% of eligible gross sales, with no minimum or further deductions.\n\nClause V2: Final eligible gross revenue USD 160,000; festival revenue earned USD 24,000. Cash collected USD 20,000, leaving USD 4,000 receivable. Final sales report dated September 1, 2026.\n\nClause V3: Projection assumed 20,000 attendees; actual attendance 16,000. This does not establish why sales fell or attribute causation to the concessionaire.', '2026-09-01T09:00:00-07:00')
    put('data/historical_contracts.json',history)
    put('data/historical_financial_entries.json',hist_lines)
    put('data/revenue_history.json',[{'id':'hist_concession_2025','counterparty_id':'partner_d','category':'beverage_concession',
      'source_document_id':'hist_concession_2025','original_projected_gross_cents':20000000,'actual_gross_cents':16000000,
      'festival_share_bps':1500,'cash_collected_cents':2000000,'status':'final','projected_attendance':20000,'actual_attendance':16000}])
    deliveries=[]
    for sid,hid in [('sup_a','hist_a_2025'),('sup_c','hist_c_2025')]:
        # Six units are one scheduled delivery, NOT six independent deliveries.
        deliveries.append({'id':hid+'_delivery','supplier_id':sid,'contract_id':hid,'scheduled_delivery_count':1,
          'qty_ordered':6,'qty_received':6,'deadline':'2025-10-16T12:00:00-07:00',
          'completed_at':('2025-10-16T11:30:00-07:00' if sid=='sup_a' else '2025-10-16T14:00:00-07:00'),
          'source_document_id':hid+'_receipt'})
        doc(hid+'_receipt','Historical delivery receipt '+hid,
          'Clause D1: Six of six generators accepted on October 16, 2025 at '+('11:30' if sid=='sup_a' else '14:00')+' America/Los_Angeles; scheduled deadline 12:00. One scheduled delivery.', '2026-09-01T09:00:00-07:00')
    put('data/historical_deliveries.json',deliveries)
    doc('cash_baseline','Procurement cash assumptions',
      'Clause C1: Opening posted bank balance at October 2, 2026 is USD 60,000. No in-flight payments. Demo cash buffer USD 50,000 is an assumption, not approved policy.\n\nClause C2: Independent committed site payment USD 5,000 due October 4. Expected sponsor receipt USD 40,000 on October 8; actual posting, if the demo advances, occurs October 9.\n\nClause C3: Compare each generator quote in a separate hypothetical scenario. No quote scenario authorizes payouts or forms an actual obligation.', '2026-10-02T08:00:00-07:00')
    put('data/cash_inputs.json',{'as_of':'2026-10-02','opening_posted_cents':6000000,'inflight_cents':0,'buffer_cents':5000000,
      'source_document_id':'cash_baseline','scheduled_outflows':[{'id':'site_balance','date':'2026-10-04','amount_cents':500000}],
      'expected_receipts':[{'id':'sponsor_expected','date':'2026-10-08','amount_cents':4000000}],
      'future_actual_receipts':[{'id':'sponsor_actual','available_at':'2026-10-09T12:00:00-07:00','date':'2026-10-09','amount_cents':4000000,'matches_expected_id':'sponsor_expected'}]})
    # A separate committed path for invoice/SLA testing, not an automatic award from comparison.
    doc('contract_b_demo','Separate authorized generator contract fixture',
      'Clause A1: Independent scripted path after a human selection and signed acceptance on October 7. Supplier sup_b, six GEN100 units, total USD 22,500, based on quote q_b_v2. PO po_gen_b has six units at USD 3,750 each. Deposit USD 8,000; final balance USD 14,500.\n\nClause A2: Deliver all six units October 15 by 12:00 America/Los_Angeles; installation accepted October 16 by 10:00. Critical support acknowledgment within 10 minutes, restoration within 60 minutes.\n\nClause A3: Supplier-caused delivery delay creates a potential service credit USD 500 per started hour, capped at USD 2,000; written notice within 24 hours of the delivery deadline is required. Festival-caused access delay is excluded to the extent supported by evidence.\n\nClause A4: Partial payment of undisputed accepted units is permitted. SLA credits require documented review and approval before allocation; no automatic deduction. Response/restoration misses have no monetary credit in this fixture.\n\nClause A5: This signed-status record is simulated, not a real agreement.', '2026-10-07T12:00:00-07:00')
    contracts=[{'id':'contract_b_demo','supplier_id':'sup_b','selected_quote_id':'q_b_v2','source_document_id':'contract_b_demo','state':'simulated_signed',
      'available_at':'2026-10-07T12:00:00-07:00','total_cents':2250000,'partial_payment_allowed':True,'po_id':'po_gen_b'}]
    put('data/authorized_contracts.json',contracts)
    put('data/obligations.json',[
      {'id':'ob_b_deposit','contract_id':'contract_b_demo','amount_cents':800000,'due_date':'2026-10-07','kind':'deposit','condition':'simulated signed contract','source_clause':'A1'},
      {'id':'ob_b_balance','contract_id':'contract_b_demo','amount_cents':1450000,'due_date':'2026-10-20','kind':'final','condition':'accepted delivery, subject to approved adjustments','source_clause':'A1'}])
    put('data/purchase_orders.json',[{'id':'po_gen_b','contract_id':'contract_b_demo','lines':[{'id':'po_gen_b_1','item_code':'GEN100','qty':6,'unit_cents':375000,'line_cents':2250000}]}])
    sla=[{'id':'sla_delivery','contract_id':'contract_b_demo','metric':'delivery_in_full','deadline':'2026-10-15T12:00:00-07:00','required_qty':6,'source_clause':'A2','remedy_clause':'A3'},
      {'id':'sla_install','contract_id':'contract_b_demo','metric':'installation_acceptance','deadline':'2026-10-16T10:00:00-07:00','source_clause':'A2'},
      {'id':'sla_ack','contract_id':'contract_b_demo','metric':'critical_acknowledgment','target_minutes':10,'source_clause':'A2'},
      {'id':'sla_restore','contract_id':'contract_b_demo','metric':'critical_restoration','target_minutes':60,'source_clause':'A2'}]
    put('data/slas.json',sla)
    paths=[
      ('late_supplier','supplier','2026-10-15T12:10:00-07:00',True),
      ('excused_access','festival','2026-10-15T12:10:00-07:00',True),
      ('missing_receipt',None,None,False),
      ('late_notice','supplier','2026-10-16T13:00:00-07:00',True)]
    observations=[]; invoices=[]
    for pid,cause,notice,has_receipt in paths:
        did=doc('evidence_'+pid,'Mutually exclusive scenario: '+pid,
          ('Clause E1: Four units accepted October 15 at 11:00; two accepted at 14:00 America/Los_Angeles.\n\n' if has_receipt else 'Clause E1: No delivery receipt is available. An invoice alone does not prove delivery.\n\n')+
          ('Clause E2: Dispatcher admits a supplier scheduling delay.\n\n' if cause=='supplier' else 'Clause E2: Site access log confirms the festival denied access from 12:00 until 14:00; dispatcher records show the remaining two units waiting at the gate at 12:00.\n\n' if cause=='festival' else 'Clause E2: Delay cause unknown.\n\n')+
          ('Clause E3: Written notice logged at '+notice+'.\n\n' if notice else 'Clause E3: No notice record.\n\n')+
          'Clause E4: Installation accepted October 16 at 09:30.\n\nClause E5: Critical incident opened October 17 at 20:00, acknowledged at 20:18, service restored at 20:50 America/Los_Angeles. No additional contractual monetary remedy for response misses.',
          '2026-10-17T21:00:00-07:00')
        observations.append({'id':pid,'exclusive_group':'delivery_alternatives','contract_id':'contract_b_demo','source_document_id':did,
          'deliveries':([{'id':pid+'_r1','qty':4,'accepted_at':'2026-10-15T11:00:00-07:00'},{'id':pid+'_r2','qty':2,'accepted_at':'2026-10-15T14:00:00-07:00'}] if has_receipt else []),
          'delay_cause':cause,'notice_at':notice,'installation_accepted_at':'2026-10-16T09:30:00-07:00',
          'incident':{'opened_at':'2026-10-17T20:00:00-07:00','acknowledged_at':'2026-10-17T20:18:00-07:00','restored_at':'2026-10-17T20:50:00-07:00'}})
    for observation in observations:
        for receipt in observation['deliveries']:
            rid=receipt['id']
            doc(rid,'Delivery acceptance '+rid,
                'Clause D1: '+str(receipt['qty'])+' GEN100 units accepted at '+receipt['accepted_at']+'. Contract contract_b_demo, PO po_gen_b.',receipt['accepted_at'])
            receipt['available_at']=receipt['accepted_at']
            receipt['source_document_id']=rid
        observation['installation_available_at']=observation['installation_accepted_at']
        observation['incident']['available_at']=observation['incident']['restored_at']
        observation['assessment_evidence_available_at']='2026-10-17T21:00:00-07:00'
    put('data/sla_observations.json',observations)
    # Invoice is intentionally due before the late second receipt; match as of 12:30.
    doc('invoice_b_001','Generator invoice',
      'Clause I1: Invoice B-001 from sup_b, issued October 15, 12:15 America/Los_Angeles. Six GEN100 units × USD 3,750 = USD 22,500, tax 0%.\n\nClause I2: Deposit USD 8,000 already paid; remaining balance requested USD 14,500. PO po_gen_b. This is a full contract invoice, not an extra USD 22,500 obligation.', '2026-10-15T12:15:00-07:00')
    put('data/invoices.json',[{'id':'inv_b_001','supplier_id':'sup_b','invoice_number':'B-001','po_id':'po_gen_b','contract_id':'contract_b_demo',
       'source_document_id':'invoice_b_001','available_at':'2026-10-15T12:15:00-07:00','total_cents':2250000,'deposit_applied_cents':800000,
       'balance_requested_cents':1450000,'lines':[{'po_line_id':'po_gen_b_1','qty':6,'unit_cents':375000}]},
      {'id':'inv_b_001_retry','supplier_id':'sup_b','invoice_number':' b-001 ','po_id':'po_gen_b','duplicate_submission_of':'inv_b_001',
       'total_cents':2250000,'source_document_id':'invoice_b_001'}])
    put('data/payments.json',[{'id':'pay_deposit_b','obligation_id':'ob_b_deposit','contract_id':'contract_b_demo','amount_cents':800000,'state':'simulated_settled','posted_at':'2026-10-07T12:30:00-07:00'}])
    put('data/risk_policy.json',{'version':'demo-v1','status':'proposed_demo_policy','not_probability_model':True,
      'rules':[{'id':'COST_VARIANCE','condition':'historical cost above original budget by >20%','label':'high','blocking':False,'requires':'explain approved changes separately'},
       {'id':'CASH_BUFFER','condition':'quote scenario closes below configured buffer','label':'high','blocking':False,'requires':'finance review'},
       {'id':'MISSING_SCOPE_PRICE','condition':'mandatory price unspecified','label':'unknown','blocking':True,'requires':'clarification before ranking'},
       {'id':'NO_HISTORY','condition':'no historical contracts','label':'unknown','blocking':False,'requires':'show no history, not zero reliability'},
       {'id':'INSURANCE_MISSING','condition':'insurance evidence absent','label':'unknown','blocking':True,'requires':'evidence before commitment'}],
      'ranking':'No opaque aggregate score; display criteria and tradeoffs. Human selection required.'})
    answer={
      'synthetic':True,'not_runtime_evidence':True,
      'procurement_as_of_2026_10_02':{
        'q_a_v1':{'comparable_total_cents':2300000,'complete':True,'minimum_closing_cents_before_receipt':5000000,'first_buffer_breach_date':None},
        'q_b_v1':{'comparable_total_cents':2100000,'complete':True,'minimum_closing_cents_before_receipt':4300000,'first_buffer_breach_date':'2026-10-05','buffer_gap_cents':700000},
        'q_c_v1':{'comparable_total_cents':None,'complete':False,'blocking_unknowns':['delivery cost','insurance evidence'],'rank_as_cheapest':False},
        'q_b_v2':{'visible':False,'reason':'not available until October 6'},
        'decision':'B is lowest complete total, but has a buffer breach and no history. A avoids a breach but has a historical overrun. C needs clarification. No uniquely correct supplier choice without human priorities.'},
      'history':{'hist_a_2025':{'net_cost_cents':2600000,'variance_vs_original_cents':600000,'variance_bps':3000,'approved_changes_cents':300000,'variance_vs_revised_cents':300000,'otif_numerator':1,'otif_denominator':1},
        'hist_c_2025':{'net_cost_cents':1950000,'variance_vs_original_cents':50000,'variance_bps':263,'approved_changes_cents':50000,'variance_vs_revised_cents':0,'otif_numerator':0,'otif_denominator':1},
        'sup_b':{'history_count':0,'otif_rate':None}},
      'revenue':{'projected_festival_share_cents':3000000,'actual_festival_share_cents':2400000,'variance_cents':-600000,'variance_bps':-2000,'collected_cents':2000000,'receivable_cents':400000,'supplier_causation_established':False},
      'quote_revision':{'q_b_v2_total_cents':2250000,'invalidates_comparison_using':'q_b_v1','requires_reapproval':True,'selection_creates_commitment':False},
      'supplier_invoice_at_2026_10_15_1230':{'scenario':'late_supplier','accepted_qty':4,'eligible_gross_cents':1500000,'disputed_gross_cents':750000,
        'prior_deposit_cents':800000,'maximum_additional_undisputed_payment_cents':700000,'full_remaining_obligation_cents':1450000,
        'later_receipt_must_not_be_used':True,'duplicate_submission_additional_payment_cents':0},
      'sla':{
        'late_supplier':{'observed_delivery':'breached','delay_minutes':120,'notice_in_time':True,'potential_credit_cents':100000,'approved_credit_cents':0},
        'excused_access':{'observed_delivery':'late','accountable_result':'excused','potential_credit_cents':0},
        'missing_receipt':{'observed_delivery':'unknown','potential_credit_cents':None,'automatic_payment_allowed':False},
        'late_notice':{'observed_delivery':'breached','notice_in_time':False,'potential_credit_cents':0,'requires_review':'Notice condition missed; do not auto-deduct.'},
        'all_paths_incident':{'ack_minutes':18,'ack_result':'breached','restoration_minutes':50,'restoration_result':'met','monetary_credit_cents':0},
        'installation':'met'},
      'guards':['Use one exclusive SLA path per run.','Only approved and issued credits reduce obligations.','Agent cannot read evaluation/answer_key.json.',
        'Do not use future quotes, receipts or actuals before available_at.','Do not add alternative quote scenarios together.',
        'One scheduled delivery is denominator 1, not denominator 6 units.','Before approved credits, obligation remaining is 1450000 cents.'],
      'proposed_mitigation_not_signed_term':'Ask B for staged payments; do not rewrite its quote until documented and approved.'}
    put('evaluation/answer_key.json',answer)
    # CSV exports for simple imports; nested values use JSON within cells.
    for path,content in list(files.items()):
        if path.startswith('data/') and path.endswith('.json'):
            rows=json.loads(content)
            if isinstance(rows,list) and rows and all(isinstance(x,dict) for x in rows):
                keys=sorted({k for r in rows for k in r});buf=io.StringIO(newline='')
                writer=csv.DictWriter(buf,fieldnames=keys,lineterminator='\n');writer.writeheader()
                for row in rows:writer.writerow({k:json.dumps(v,ensure_ascii=False,sort_keys=True) if isinstance(v,(list,dict)) else v for k,v in row.items()})
                files['csv/'+Path(path).stem+'.csv']=buf.getvalue()
    put('data/source_index.json',docs)
    put('manifest.json',{'version':'1.0.0','seed':seed,'synthetic':True,'currency':'USD','money_unit':'integer cents','rate_unit':'basis points',
      'date_timezone':'America/Los_Angeles','random_sample_id':rng.randrange(100000,999999),
      'files':{p:hashlib.sha256(v.encode()).hexdigest() for p,v in sorted(files.items())}})
    return files

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',default='generated');p.add_argument('--seed',type=int,default=20261024);a=p.parse_args()
    out=Path(a.output);out.mkdir(parents=True,exist_ok=True)
    for name,content in generate(a.seed).items():
        dest=out/name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_text(content,encoding='utf-8')
    print('Generated synthetic Encore fixtures:',out)
