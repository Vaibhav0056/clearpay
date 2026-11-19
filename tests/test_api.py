from fastapi.testclient import TestClient
from invoiceiq.api import app

def test_health():
    client=TestClient(app)
    assert client.get('/health').json()['ok']

def test_api_end_to_end(tmp_path,monkeypatch):
    from invoiceiq.database import Repository
    from pathlib import Path
    db=str(tmp_path/'api.sqlite')
    monkeypatch.setenv('INVOICEIQ_DB',db)
    Repository(db).seed()
    payload=(Path(__file__).parent.parent/'fixtures/invoices/01_happy_path.pdf').read_bytes()
    client=TestClient(app)
    response=client.post('/process',content=payload,headers={'content-type':'application/pdf','x-filename':'happy.pdf'})
    assert response.status_code==200,response.text
    assert response.json()['decision']['status']=='APPROVED'
    assert client.get('/runs').json()[0]['status']=='APPROVED'
