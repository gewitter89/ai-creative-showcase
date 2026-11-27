import ast
import contextlib,sqlite3,tempfile,unittest,threading
from pathlib import Path

def utc_now_iso():return '2026-11-26T21:00:00Z'
class DB:
 def __init__(self,path):self.path=path
 @contextlib.contextmanager
 def _connect(self):
  with contextlib.closing(sqlite3.connect(self.path,timeout=5)) as conn:
   conn.row_factory=sqlite3.Row
   with conn:yield conn
 def _log_audit_conn(self,c,event_type,**kw):c.execute('INSERT INTO audit VALUES(?)',(event_type,))
ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / 'scripts/store_db.py').read_text(encoding='utf-8')
cls = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == 'StoreDB')
method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'record_successful_payment')
exec('class Candidate(DB):\n' + '\n'.join(source.splitlines()[method.lineno-1:method.end_lineno]) + '\n')
class Tests(unittest.TestCase):
 def setUp(self):
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.path=Path(t.name)/'db';self.s=Candidate(self.path)
  with self.s._connect() as c:
   c.executescript('''CREATE TABLE orders(order_id TEXT PRIMARY KEY,user_id TEXT,product_id TEXT,amount_xtr INTEGER,invoice_payload TEXT UNIQUE,status TEXT,updated_at TEXT);CREATE TABLE payments(order_id TEXT,user_id TEXT,currency TEXT,total_amount INTEGER,invoice_payload TEXT,telegram_payment_charge_id TEXT UNIQUE,provider_payment_charge_id TEXT,status TEXT,created_at TEXT);CREATE TABLE audit(event TEXT);INSERT INTO orders VALUES('o','123','ny-kyiv-01',100,'p','pre_checkout_ok','');''')
 def pay(self,charge='c',user='123',amount=100,payload='p',currency='XTR',provider=''):
  return self.s.record_successful_payment(user,currency,amount,payload,charge,provider)[0]
 def counts(self):
  with self.s._connect() as c:return c.execute('SELECT COUNT(*) FROM payments').fetchone()[0],c.execute('SELECT COUNT(*) FROM payment_review').fetchone()[0],c.execute('SELECT status FROM orders').fetchone()[0]
 def test_valid(self):self.assertEqual(self.pay(),'recorded');self.assertEqual(self.counts(),(1,0,'paid'))
 def test_replay_after_reopen(self):self.pay();self.s=Candidate(self.path);self.assertEqual(self.pay(),'duplicate_charge');self.assertEqual(self.counts(),(1,0,'paid'))
 def test_other_buyer_replay(self):self.pay();self.assertEqual(self.pay(user='456'),'mismatch');self.assertEqual(self.counts(),(1,0,'paid'))
 def test_wrong_amount_replay(self):self.pay();self.assertEqual(self.pay(amount=101),'mismatch')
 def test_wrong_provider_replay(self):self.pay();self.assertEqual(self.pay(provider='changed'),'mismatch')
 def test_second_charge(self):self.pay();self.assertEqual(self.pay(charge='c2'),'review_required');self.assertEqual(self.counts(),(1,1,'paid'));self.assertEqual(self.pay(charge='c2'),'review_required')
 def test_wrong_payload_persisted(self):self.assertEqual(self.pay(payload='bad'),'review_required');self.assertEqual(self.counts(),(0,1,'pre_checkout_ok'));self.assertEqual(self.pay(),'review_required')
 def test_unexpected_status(self):
  with self.s._connect() as c:c.execute("UPDATE orders SET status='created'")
  self.assertEqual(self.pay(),'review_required');self.assertEqual(self.counts(),(0,1,'created'))
 def test_refunded_replay(self):
  self.pay()
  with self.s._connect() as c:c.execute("UPDATE payments SET status='refunded'");c.execute("UPDATE orders SET status='refunded'")
  self.assertEqual(self.pay(),'review_required');self.assertEqual(self.counts(),(1,0,'refunded'))
 def test_concurrent(self):
  b=threading.Barrier(2);res=[]
  def work(ch):b.wait(5);res.append(self.pay(charge=ch))
  a,t=threading.Thread(target=work,args=('c1',)),threading.Thread(target=work,args=('c2',));a.start();t.start();a.join(6);t.join(6);self.assertEqual(sorted(res),['recorded','review_required']);self.assertEqual(self.counts(),(1,1,'paid'))
 def test_rollback_on_audit_failure(self):
  self.s._log_audit_conn=lambda *a,**k:(_ for _ in ()).throw(OSError('failure'))
  with self.assertRaises(OSError):self.pay()
  self.s=Candidate(self.path);self.assertEqual(self.pay(),'recorded');self.assertEqual(self.counts(),(1,0,'paid'))
if __name__=='__main__':unittest.main()
