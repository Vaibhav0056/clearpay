from pathlib import Path
from invoiceiq.workflow import process_pdf
from invoiceiq.schema import money
from invoiceiq.retrieval import chunk_policy,PolicySearch
from invoiceiq.extraction import parse_document,extract_fixture_format

ROOT=Path(__file__).parent.parent

def test_money_locales():
    assert money('1,234.56')==money('1234.56')
    assert money('1.234,56')==money('1234.56')
    assert money('1,234')==money('1234')


def test_policy_chunking(tmp_path):
    d=tmp_path/'policies';d.mkdir()
    (d/'policy.md').write_text('# Tax\nTax inclusive and exclusive rules.\n# Duplicate\nDuplicate invoices must be blocked.',encoding='utf8')
    result=PolicySearch(d).search('duplicate invoices')
    assert result and result[0]['section']=='Duplicate'
    assert len(chunk_policy('# Alpha\na b c\n# Beta\nx y', 'a'))==2


def test_real_pdf_end_to_end(repo):
    file=ROOT/'fixtures/invoices/01_happy_path.pdf'
    pdf=file.read_bytes()
    doc=parse_document(pdf)
    assert not doc.scanned
    invoice=extract_fixture_format(doc.text)
    assert invoice.invoice_number=='INV-001'
    result=process_pdf(pdf,file.name,repo,mode='fixture',policy_dir=ROOT/'fixtures/policies',graph_enabled=False)
    assert result['decision'].status=='APPROVED'
    assert repo.get_run(result['run_id'])['status']=='APPROVED'
    again=process_pdf(pdf,file.name,repo,mode='fixture',policy_dir=ROOT/'fixtures/policies',graph_enabled=False)
    assert again['decision'].status=='BLOCKED'


def test_error_persists_run(repo):
    try:
        process_pdf(b'not a pdf','bad.pdf',repo,mode='fixture',graph_enabled=False)
        assert False
    except ValueError:
        pass
    assert repo.runs()[0]['status']=='ERROR'


def test_missing_po_collects_policy_and_candidates(repo):
    file=ROOT/'fixtures/invoices/05_missing_po.pdf'
    result=process_pdf(file.read_bytes(),file.name,repo,mode='fixture',policy_dir=ROOT/'fixtures/policies',graph_enabled=False)
    assert result['decision'].status=='NEEDS_REVIEW'
    sources=[x['source'] for x in result['decision'].evidence['retrieval']]
    assert 'po_candidates' in sources
    assert 'semantic_po_line_candidates' in sources
    assert 'ap_policy.md' in sources


def test_human_review_can_correct_without_bypass(repo):
    from invoiceiq.workflow import review_and_recheck
    file=ROOT/'fixtures/invoices/05_missing_po.pdf'
    r=process_pdf(file.read_bytes(),file.name,repo,mode='fixture',policy_dir=ROOT/'fixtures/policies',graph_enabled=False)
    assert r['decision'].status=='NEEDS_REVIEW'
    data=r['invoice'].model_dump(mode='json')
    data['po_number']='PO-1007'
    corrected=review_and_recheck(repo,r['run_id'],data)
    assert corrected['decision'].status=='APPROVED'
    assert corrected['decision'].evidence['parent_run_id']==r['run_id']
    assert repo.get_run(r['run_id'])['status']=='NEEDS_REVIEW'
    # A second review of the original may not silently create another payable.
    repeat=review_and_recheck(repo,r['run_id'],data)
    assert repeat['decision'].status=='BLOCKED'
