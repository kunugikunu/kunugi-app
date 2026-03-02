#!/usr/bin/env python3
"""
㓛刀工業 作業管理システム - バックエンドサーバー（ログイン機能付き）
Python標準ライブラリのみ（追加インストール不要）
起動: python3 server.py
"""

import sqlite3, json, os, hashlib, secrets, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from datetime import datetime

PORT    = int(os.environ.get("PORT", 8000))
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kukito.db")

SESSIONS  = {}
SESSION_TTL = 60 * 60 * 24 * 7  # 7日間

def hash_pw(pw):   return hashlib.sha256(pw.encode()).hexdigest()
def new_token():   return secrets.token_hex(32)

def get_session(token):
    if not token: return None
    s = SESSIONS.get(token)
    if not s: return None
    if time.time() > s["expires"]:
        del SESSIONS[token]; return None
    return s

# ── DB ───────────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS employees (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT DEFAULT '従業員',
        hourly INTEGER DEFAULT 1800, allowance INTEGER DEFAULT 0,
        password TEXT DEFAULT '', role TEXT DEFAULT 'employee',
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS sites (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, client TEXT,
        site_type TEXT DEFAULT '請負',
        contract INTEGER DEFAULT 0, budget INTEGER DEFAULT 0,
        manday_price INTEGER DEFAULT 0,
        extra_amount INTEGER DEFAULT 0,
        status TEXT DEFAULT '準備中',
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS extra_works (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        site_id TEXT NOT NULL,
        date TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        amount INTEGER NOT NULL DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (site_id) REFERENCES sites(id)
    );
    CREATE TABLE IF NOT EXISTS daily_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL, emp_id TEXT NOT NULL, site_id TEXT NOT NULL,
        start_time TEXT NOT NULL, end_time TEXT NOT NULL,
        rest_min INTEGER DEFAULT 60, allowances TEXT DEFAULT '[]', memo TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL, site_id TEXT NOT NULL, category TEXT NOT NULL,
        amount INTEGER DEFAULT 0, payment TEXT DEFAULT '現金', memo TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS subcons (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL, vendor TEXT NOT NULL, site_id TEXT NOT NULL,
        work TEXT NOT NULL, qty REAL DEFAULT 1, unit TEXT DEFAULT '人工',
        price INTEGER DEFAULT 0, status TEXT DEFAULT '未払',
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    """)
    # マイグレーション（既存DBにpassword/roleカラムがない場合）
    cols = [r[1] for r in cur.execute("PRAGMA table_info(employees)")]
    if "password" not in cols: cur.execute("ALTER TABLE employees ADD COLUMN password TEXT DEFAULT ''")
    if "role" not in cols:     cur.execute("ALTER TABLE employees ADD COLUMN role TEXT DEFAULT 'employee'")

    # マイグレーション（既存sitesテーブルへのカラム追加）
    site_cols = [r[1] for r in cur.execute("PRAGMA table_info(sites)")]
    if "site_type"    not in site_cols: cur.execute("ALTER TABLE sites ADD COLUMN site_type TEXT DEFAULT '請負'")
    if "manday_price" not in site_cols: cur.execute("ALTER TABLE sites ADD COLUMN manday_price INTEGER DEFAULT 0")
    if "extra_amount" not in site_cols: cur.execute("ALTER TABLE sites ADD COLUMN extra_amount INTEGER DEFAULT 0")

    if cur.execute("SELECT COUNT(*) FROM employees").fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO employees (id,name,type,hourly,allowance,password,role) VALUES (?,?,?,?,?,?,?)", [
            ("MGR",  "㓛刀 代表",  "従業員", 0,    0,    hash_pw("admin1234"), "manager"),
            ("E001", "田中 太郎",  "従業員", 1800, 3000, hash_pw("tanaka123"), "employee"),
            ("E002", "山田 花子",  "従業員", 1600, 3000, hash_pw("yamada123"), "employee"),
        ])
        cur.executemany("INSERT INTO sites (id,name,client,contract,budget,site_type,manday_price,extra_amount,status) VALUES (?,?,?,?,?,?,?,?,?)", [
            ("S001","〇〇ビル新築工事","〇〇建設",  5000000,3500000,"請負",0,0,"進行中"),
            ("S002","△△マンション改修","△△不動産", 3000000,2000000,"請負",0,0,"進行中"),
            ("S003","□□倉庫電気工事","□□物流",   0,0,"応援",25000,0,"進行中"),
            ("S004","◇◇住宅リフォーム","直接受注",  1200000, 900000,"請負",0,0,"準備中"),
        ])
        td = datetime.now().strftime("%Y-%m-%d")
        cur.executemany("INSERT INTO daily_logs (date,emp_id,site_id,start_time,end_time,rest_min,allowances,memo) VALUES (?,?,?,?,?,?,?,?)", [
            (td,"E001","S001","08:00","17:00",60,"[]",""),
            (td,"E002","S001","08:00","16:00",60,"[]",""),
        ])
        cur.executemany("INSERT INTO expenses (date,site_id,category,amount,payment,memo) VALUES (?,?,?,?,?,?)", [
            (td,"S001","ガソリン",8500,"クレカ",""),
            (td,"S001","高速代",2300,"ETC",""),
            (td,"S002","材料費",45000,"振込",""),
        ])
        cur.executemany("INSERT INTO subcons (date,vendor,site_id,work,qty,unit,price,status) VALUES (?,?,?,?,?,?,?,?)", [
            (td,"A社","S001","型枠工事",3,"人工",25000,"未払"),
            (td,"B社","S002","左官工事",2,"人工",22000,"未払"),
            (td,"C社","S003","設備工事",1,"一式",150000,"未払"),
        ])
    con.commit(); con.close()
    print(f"✅ DB初期化完了: {DB_PATH}")

def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def rows(r): return [dict(x) for x in r]
def row(r):  return dict(r) if r else None

# ── Handler ───────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {fmt%args}")

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET,POST,PUT,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Content-Type,Authorization")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path):
        with open(path,"rb") as f: data=f.read()
        self.send_response(200)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("Content-Length",len(data))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length",0))
        return json.loads(self.rfile.read(n)) if n else {}

    def token(self):
        return self.headers.get("Authorization","").replace("Bearer ","").strip() or None

    def auth(self, mgr=False):
        s = get_session(self.token())
        if not s:           self.send_json({"error":"ログインが必要です"},401); return None
        if mgr and s["role"]!="manager": self.send_json({"error":"代表権限が必要です"},403); return None
        return s

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET,POST,PUT,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Content-Type,Authorization")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")
        if path in ("","/"): self.send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)),"index.html")); return

        con = get_db()
        try:
            if path=="/api/me":
                s=get_session(self.token())
                if not s: self.send_json({"error":"未ログイン"},401); return
                r=con.execute("SELECT id,name,type,role FROM employees WHERE id=?",(s["emp_id"],)).fetchone()
                self.send_json(row(r))

            elif path=="/api/employees":
                if not self.auth(mgr=True): return
                self.send_json(rows(con.execute("SELECT id,name,type,hourly,allowance,role,created_at FROM employees ORDER BY id").fetchall()))

            elif path=="/api/sites":
                if not self.auth(): return
                self.send_json(rows(con.execute("SELECT * FROM sites ORDER BY id").fetchall()))

            elif path=="/api/logs":
                s=self.auth()
                if not s: return
                if s["role"]=="manager":
                    r=con.execute("SELECT dl.*,e.name emp_name,e.hourly,s.name site_name FROM daily_logs dl LEFT JOIN employees e ON dl.emp_id=e.id LEFT JOIN sites s ON dl.site_id=s.id ORDER BY dl.date DESC,dl.id DESC").fetchall()
                else:
                    r=con.execute("SELECT dl.*,e.name emp_name,e.hourly,s.name site_name FROM daily_logs dl LEFT JOIN employees e ON dl.emp_id=e.id LEFT JOIN sites s ON dl.site_id=s.id WHERE dl.emp_id=? ORDER BY dl.date DESC,dl.id DESC",(s["emp_id"],)).fetchall()
                self.send_json(rows(r))

            elif path=="/api/expenses":
                if not self.auth(mgr=True): return
                self.send_json(rows(con.execute("SELECT ex.*,s.name site_name FROM expenses ex LEFT JOIN sites s ON ex.site_id=s.id ORDER BY ex.date DESC,ex.id DESC").fetchall()))

            elif path=="/api/subcons":
                if not self.auth(mgr=True): return
                self.send_json(rows(con.execute("SELECT sc.*,s.name site_name FROM subcons sc LEFT JOIN sites s ON sc.site_id=s.id ORDER BY sc.date DESC,sc.id DESC").fetchall()))

            elif path=="/api/summary":
                if not self.auth(mgr=True): return
                sites = rows(con.execute("SELECT * FROM sites ORDER BY id").fetchall())
                for st in sites:
                    sid=st["id"]
                    logs=con.execute("SELECT dl.start_time,dl.end_time,dl.rest_min,e.hourly FROM daily_logs dl JOIN employees e ON dl.emp_id=e.id WHERE dl.site_id=?",(sid,)).fetchall()
                    labor=0; mandays=len(logs)
                    for l in logs:
                        sh,sm=map(int,l["start_time"].split(":")); eh,em=map(int,l["end_time"].split(":"))
                        ac=max(0,(eh*60+em-sh*60-sm-l["rest_min"])/60); ot=max(0,ac-8)
                        if l["hourly"]>0:
                            labor+=round(min(ac,8)*l["hourly"]+ot*l["hourly"]*1.25)
                    # 売上計算：応援は人工×単価、請負は契約金額＋追加工事
                    if st.get("site_type")=="応援":
                        revenue = mandays * st.get("manday_price",0)
                    else:
                        extra = con.execute("SELECT COALESCE(SUM(amount),0) t FROM extra_works WHERE site_id=?",(sid,)).fetchone()["t"]
                        revenue = st["contract"] + extra
                        st["extra_amount"] = extra
                    profit = revenue - labor
                    cost_per_manday = round(labor/mandays) if mandays>0 else 0
                    revenue_per_manday = round(revenue/mandays) if mandays>0 else 0
                    st.update({"revenue":revenue,"labor_cost":labor,"total_cost":labor,"profit":profit,
                               "profit_rate":round(profit/revenue,4) if revenue>0 else 0,
                               "mandays":mandays,"cost_per_manday":cost_per_manday,"revenue_per_manday":revenue_per_manday})
                self.send_json(sites)

            elif path=="/api/extra_works":
                if not self.auth(mgr=True): return
                site_id = urlparse(self.path).query.replace("site_id=","") if "site_id=" in self.path else None
                if site_id:
                    r=con.execute("SELECT * FROM extra_works WHERE site_id=? ORDER BY date DESC",(site_id,)).fetchall()
                else:
                    r=con.execute("SELECT ew.*,s.name site_name FROM extra_works ew LEFT JOIN sites s ON ew.site_id=s.id ORDER BY ew.date DESC").fetchall()
                self.send_json(rows(r))

            elif path=="/api/salary":
                if not self.auth(mgr=True): return
                emps=rows(con.execute("SELECT * FROM employees WHERE type='従業員' ORDER BY id").fetchall())
                for e in emps:
                    ls=con.execute("SELECT * FROM daily_logs WHERE emp_id=?",(e["id"],)).fetchall()
                    nh=oh=w=0
                    for l in ls:
                        sh,sm=map(int,l["start_time"].split(":")); eh,em=map(int,l["end_time"].split(":"))
                        ac=max(0,(eh*60+em-sh*60-sm-l["rest_min"])/60); ot=max(0,ac-8)
                        nh+=ac-ot; oh+=ot; w+=round(min(ac,8)*e["hourly"]+ot*e["hourly"]*1.25)
                    e.update({"normal_h":round(nh,2),"ot_h":round(oh,2),"wage":w,"days":len(ls)})
                    e.pop("password",None)
                self.send_json(emps)

            else: self.send_json({"error":"Not found"},404)
        except Exception as e:
            import traceback; traceback.print_exc()
            self.send_json({"error":str(e)},500)
        finally: con.close()

    def do_POST(self):
        path=urlparse(self.path).path.rstrip("/")
        b=self.body(); con=get_db()
        try:
            if path=="/api/login":
                r=con.execute("SELECT * FROM employees WHERE id=?",(b.get("empId",""),)).fetchone()
                if not r or r["password"]!=hash_pw(b.get("password","")):
                    self.send_json({"error":"IDまたはパスワードが違います"},401); return
                tk=new_token()
                SESSIONS[tk]={"emp_id":r["id"],"role":r["role"],"expires":time.time()+SESSION_TTL}
                self.send_json({"token":tk,"empId":r["id"],"name":r["name"],"role":r["role"]}); return

            if path=="/api/logout":
                SESSIONS.pop(self.token(),None); self.send_json({"ok":True}); return

            if path=="/api/employees":
                if not self.auth(mgr=True): return
                con.execute("INSERT INTO employees (id,name,type,hourly,allowance,password,role) VALUES (?,?,?,?,?,?,?)",
                    (b["id"],b["name"],b.get("type","従業員"),int(b.get("hourly",1800)),int(b.get("allowance",0)),
                     hash_pw(b.get("password","password1234")),b.get("role","employee")))
                con.commit()
                r=con.execute("SELECT id,name,type,hourly,allowance,role FROM employees WHERE id=?",(b["id"],)).fetchone()
                self.send_json(row(r),201); return

            s=self.auth()
            if not s: return

            if path=="/api/sites":
                if s["role"]!="manager": self.send_json({"error":"権限なし"},403); return
                con.execute("INSERT INTO sites (id,name,client,site_type,contract,budget,manday_price,extra_amount,status) VALUES (?,?,?,?,?,?,?,?,?)",
                    (b["id"],b["name"],b.get("client",""),b.get("site_type","請負"),
                     int(b.get("contract",0)),int(b.get("budget",0)),
                     int(b.get("manday_price",0)),0,b.get("status","準備中")))
                con.commit()
                self.send_json(row(con.execute("SELECT * FROM sites WHERE id=?",(b["id"],)).fetchone()),201)

            elif path=="/api/extra_works":
                if s["role"]!="manager": self.send_json({"error":"権限なし"},403); return
                cur=con.execute("INSERT INTO extra_works (site_id,date,description,amount) VALUES (?,?,?,?)",
                    (b["siteId"],b["date"],b.get("description","追加工事"),int(b.get("amount",0))))
                con.commit()
                self.send_json(row(con.execute("SELECT * FROM extra_works WHERE id=?",(cur.lastrowid,)).fetchone()),201)

            elif path=="/api/logs":
                eid = b.get("empId",s["emp_id"]) if s["role"]=="manager" else s["emp_id"]
                cur=con.execute("INSERT INTO daily_logs (date,emp_id,site_id,start_time,end_time,rest_min,allowances,memo) VALUES (?,?,?,?,?,?,?,?)",
                    (b["date"],eid,b["siteId"],b["start"],b["end"],int(b.get("rest",60)),
                     json.dumps(b.get("allowances",[]),ensure_ascii=False),b.get("memo","")))
                con.commit()
                self.send_json(row(con.execute("SELECT * FROM daily_logs WHERE id=?",(cur.lastrowid,)).fetchone()),201)

            elif path=="/api/expenses":
                if s["role"]!="manager": self.send_json({"error":"権限なし"},403); return
                cur=con.execute("INSERT INTO expenses (date,site_id,category,amount,payment,memo) VALUES (?,?,?,?,?,?)",
                    (b["date"],b["siteId"],b["category"],int(b.get("amount",0)),b.get("payment","現金"),b.get("memo","")))
                con.commit()
                self.send_json(row(con.execute("SELECT * FROM expenses WHERE id=?",(cur.lastrowid,)).fetchone()),201)

            elif path=="/api/subcons":
                if s["role"]!="manager": self.send_json({"error":"権限なし"},403); return
                cur=con.execute("INSERT INTO subcons (date,vendor,site_id,work,qty,unit,price,status) VALUES (?,?,?,?,?,?,?,?)",
                    (b["date"],b["vendor"],b["siteId"],b.get("work",""),float(b.get("qty",1)),b.get("unit","人工"),int(b.get("price",0)),b.get("status","未払")))
                con.commit()
                self.send_json(row(con.execute("SELECT * FROM subcons WHERE id=?",(cur.lastrowid,)).fetchone()),201)

            else: self.send_json({"error":"Not found"},404)

        except sqlite3.IntegrityError as e: self.send_json({"error":f"IDが重複しています:{e}"},409)
        except Exception as e:
            import traceback; traceback.print_exc()
            self.send_json({"error":str(e)},500)
        finally: con.close()

    def do_PUT(self):
        parts=urlparse(self.path).path.strip("/").split("/")
        b=self.body(); s=self.auth(mgr=True)
        if not s: return
        con=get_db()
        try:
            if len(parts)==3:
                if parts[1]=="subcons": con.execute("UPDATE subcons SET status=? WHERE id=?",(b["status"],parts[2]))
                elif parts[1]=="sites":
                    # 更新できるフィールドを選択的に適用
                    fields=[]; vals=[]
                    for k in ["name","client","site_type","contract","manday_price","status"]:
                        if k in b:
                            fields.append(f"{k}=?")
                            vals.append(int(b[k]) if k in ["contract","manday_price"] else b[k])
                    if fields:
                        vals.append(parts[2])
                        con.execute(f"UPDATE sites SET {','.join(fields)} WHERE id=?", vals)
                    con.commit()
                    self.send_json(row(con.execute("SELECT * FROM sites WHERE id=?",(parts[2],)).fetchone())); return

                elif parts[1]=="extra_works": con.execute("DELETE FROM extra_works WHERE id=?",(parts[2],)); con.commit(); self.send_json({"deleted":parts[2]}); return
                elif parts[1]=="employees":
                    if "password" in b: con.execute("UPDATE employees SET password=? WHERE id=?",(hash_pw(b["password"]),parts[2]))
                    if "hourly"   in b: con.execute("UPDATE employees SET hourly=? WHERE id=?",(int(b["hourly"]),parts[2]))
                    con.commit()
                    self.send_json(row(con.execute("SELECT id,name,type,hourly,allowance,role FROM employees WHERE id=?",(parts[2],)).fetchone())); return
                else: self.send_json({"error":"Not found"},404); return
                con.commit(); self.send_json({"ok":True})
            else: self.send_json({"error":"Not found"},404)
        except Exception as e: self.send_json({"error":str(e)},500)
        finally: con.close()

    def do_DELETE(self):
        parts=urlparse(self.path).path.strip("/").split("/")
        s=self.auth()
        if not s: return
        con=get_db()
        try:
            if len(parts)==3:
                tmap={"employees":"employees","sites":"sites","logs":"daily_logs","expenses":"expenses","subcons":"subcons","extra_works":"extra_works"}
                tbl=tmap.get(parts[1])
                if not tbl: self.send_json({"error":"Not found"},404); return
                if parts[1]=="logs" and s["role"]!="manager":
                    r=con.execute("SELECT emp_id FROM daily_logs WHERE id=?",(parts[2],)).fetchone()
                    if not r or r["emp_id"]!=s["emp_id"]: self.send_json({"error":"権限なし"},403); return
                elif parts[1]!="logs" and s["role"]!="manager":
                    self.send_json({"error":"権限なし"},403); return
                con.execute(f"DELETE FROM {tbl} WHERE id=?",(parts[2],)); con.commit()
                self.send_json({"deleted":parts[2]})
            else: self.send_json({"error":"Not found"},404)
        except Exception as e: self.send_json({"error":str(e)},500)
        finally: con.close()

if __name__=="__main__":
    init_db()
    server=HTTPServer(("0.0.0.0",PORT),Handler)
    print(f"""
╔══════════════════════════════════════════════╗
║   㓛刀工業 作業管理システム（ログイン対応）   ║
╠══════════════════════════════════════════════╣
║  URL  : http://localhost:{PORT:<19}║
╚══════════════════════════════════════════════╝
初期ログイン情報:
  代表  ID: MGR   PW: admin1234
  田中  ID: E001  PW: tanaka123
  山田  ID: E002  PW: yamada123
""")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止"); server.server_close()
