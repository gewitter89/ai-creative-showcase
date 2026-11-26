import ast
import contextlib
import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
from typing import Callable
import unittest
import zipfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from package_version import staged_order_package

# Fixture only: candidate methods are executed verbatim; other store methods are
# small SQLite stand-ins. No Telegram or production store is involved.
ROOT = Path(__file__).resolve().parents[1]

def utc_now_iso():
    return '2026-11-26T21:00:00+00:00'

class StoreDB:
    def __init__(self, db):
        self.db = db
    @contextlib.contextmanager
    def _connect(self):
        with contextlib.closing(sqlite3.connect(self.db, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            with conn:
                yield conn
    def _log_audit_conn(self, conn, event_type, **kw):
        conn.execute('INSERT INTO events VALUES (?)', (event_type,))
    def log_audit(self, event_type, **kw):
        with self._connect() as c:
            self._log_audit_conn(c, event_type, **kw)
    def get_order_package_version(self, order):
        with self._connect() as c:
            r = c.execute('SELECT * FROM product_versions WHERE product_id=? AND version=?', (order['product_id'], order['product_version'])).fetchone()
            return dict(r) if r else None
    def get_delivery_by_order(self, order_id):
        with self._connect() as c:
            r = c.execute('SELECT * FROM deliveries WHERE order_id=?', (order_id,)).fetchone()
            return dict(r) if r else None
    def mark_delivery_sent(self, order_id, telegram_file_id):
        with self._connect() as c:
            c.execute("UPDATE deliveries SET status='sent' WHERE order_id=?", (order_id,))
            c.execute("UPDATE orders SET status='delivered' WHERE order_id=?", (order_id,))

# Assemble the exact candidate method, not a reimplementation.
def source_node(path, name, class_name=None):
    source = path.read_text(encoding='utf-8')
    tree = ast.parse(source)
    body = tree.body
    if class_name:
        body = next(n.body for n in body if isinstance(n, ast.ClassDef) and n.name == class_name)
    node = next(n for n in body if isinstance(n, ast.FunctionDef) and n.name == name)
    return '\n'.join(source.splitlines()[node.lineno - 1:node.end_lineno]) + '\n'

exec('class Candidate(StoreDB):\n' + source_node(ROOT/'scripts/store_db.py', 'reserve_paid_delivery', 'StoreDB'), globals())
BotConfig = SimpleNamespace
_resolved_packages_dir = lambda cfg: cfg.packages_dir
exec(source_node(ROOT/'scripts/studio_bot.py', '_deliver_paid_order'), globals())

class DeliveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root/'ny-kyiv-01.zip'
        with zipfile.ZipFile(self.source,'w') as z:
            z.writestr('README.txt','purchased')
        self.data = self.source.read_bytes()
        self.order = dict(order_id='order1', user_id='12345', chat_id='12345', product_id='ny-kyiv-01', product_version=1)
        self.version = dict(product_id='ny-kyiv-01',version=1,package_path=str(self.source),package_bytes=len(self.data),package_sha256=hashlib.sha256(self.data).hexdigest())
        self.store = Candidate(self.root/'store.db')
        with self.store._connect() as c:
            c.executescript('''CREATE TABLE orders(order_id TEXT PRIMARY KEY,user_id TEXT,chat_id TEXT,product_id TEXT,product_version INTEGER,status TEXT,currency TEXT,amount_xtr INTEGER);
            CREATE TABLE product_versions(product_id TEXT,version INTEGER,package_path TEXT,package_sha256 TEXT,package_bytes INTEGER);
            CREATE TABLE payments(order_id TEXT,user_id TEXT,status TEXT,currency TEXT,total_amount INTEGER);
            CREATE TABLE deliveries(order_id TEXT UNIQUE,user_id TEXT,product_id TEXT,package_path TEXT,package_sha256 TEXT,status TEXT,created_at TEXT);
            CREATE TABLE events(event TEXT);''')
            c.execute("INSERT INTO orders VALUES (?,?,?,?,?,'paid','XTR',100)",tuple(self.order.values()))
            c.execute('INSERT INTO product_versions VALUES (?,?,?,?,?)',(self.version['product_id'],1,str(self.source),self.version['package_sha256'],len(self.data)))
            c.execute("INSERT INTO payments VALUES ('order1','12345','paid','XTR',100)")
        self.cfg = SimpleNamespace(token='fixture',packages_dir=self.root)
        self.uploads=[]
    def send(self,token,chat,path,caption):
        self.uploads.append(path.read_bytes())
        return {'ok':True,'result':{'document':{'file_id':'fixture'}}}
    def api(self,*a): return {'ok':True}
    def deliver(self,send=None,api=None,chat=12345):
        _deliver_paid_order(self.order,chat,self.cfg,self.store,api or self.api,send or self.send)
    def state(self):
        r=self.store.get_delivery_by_order('order1')
        return r['status'] if r else None
    def test_source_change_at_upload(self):
        def swap(token,chat,path,caption):
            self.source.write_bytes(b'wrong')
            self.assertEqual(path.read_bytes(),self.data)
            return self.send(token,chat,path,caption)
        self.deliver(send=swap);self.assertEqual(self.state(),'sent');self.assertEqual(self.uploads,[self.data])
    def test_uncertain_upload_no_replay(self):
        def timeout(*args):
            self.uploads.append(b'uncertain');raise TimeoutError()
        self.deliver(send=timeout);self.assertEqual(self.state(),'sending')
        self.deliver();self.assertEqual(self.uploads,[b'uncertain'])
    def test_notification_failure_preserves_sent(self):
        def failure(*args): raise TimeoutError()
        self.deliver(api=failure);self.assertEqual(self.state(),'sent');self.deliver();self.assertEqual(len(self.uploads),1)
    def test_wrong_chat_no_delivery(self):
        self.deliver(chat=99999);self.assertIsNone(self.state())
    def test_refunded_no_delivery(self):
        with self.store._connect() as c:c.execute("UPDATE payments SET status='refunded'")
        self.deliver();self.assertIsNone(self.state());self.assertEqual(self.uploads,[])
    def test_no_payment_no_delivery(self):
        with self.store._connect() as c:c.execute('DELETE FROM payments')
        self.deliver();self.assertIsNone(self.state())
    def test_reservation_survives_restart(self):
        self.assertTrue(self.store.reserve_paid_delivery(self.order,self.version))
        self.store=Candidate(self.root/'store.db');self.deliver();self.assertEqual(self.state(),'sending');self.assertEqual(self.uploads,[])
    def test_concurrent_claim(self):
        barrier=threading.Barrier(2);results=[]
        def claim():
            barrier.wait(timeout=5);results.append(Candidate(self.root/'store.db').reserve_paid_delivery(self.order,self.version))
        a,b=threading.Thread(target=claim),threading.Thread(target=claim)
        a.start();b.start();a.join(6);b.join(6)
        self.assertEqual(sorted(results),[False,True])
    def test_changed_before_stage(self):
        self.source.write_bytes(b'wrong');self.deliver();self.assertIsNone(self.state());self.assertEqual(self.uploads,[])

if __name__=='__main__': unittest.main()
