#!/usr/bin/env python3
"""㓛刀工業 作業管理システム"""

import sqlite3, json, os, hashlib, secrets, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime

PORT    = int(os.environ.get("PORT", 8000))
DB_PATH = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", os.path.dirname(os.path.abspath(__file__))) + "/kukito.db"

SESSIONS    = {}
SESSION_TTL = 60 * 60 * 24 * 7

# 手当単価（固定）
OT_HOURLY    = 2100   # 時間外 円/h
DRIVE_PER_KM = 4      # 運転手当 円/km
MOVE_PER_KM  = 14     # 移動手当 円/km

def hash_pw(pw):  return hashlib.sha256(pw.encode()).hexdigest()
def new_token():  return secrets.token_hex(32)

def get_session(token):
    if not token: return None
    s = SESSIONS.get(token)
    if not s: return None
    if time.time() > s["expires"]: del SESSIONS[token]; return None
    return s

# ── DB ───────────────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS employees (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT DEFAULT '従業員',
        daily_wage INTEGER DEFAULT 0,
        password TEXT DEFAULT '', role TEXT DEFAULT 'employee',
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS sites (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, client TEXT DEFAULT '',
        site_type TEXT DEFAULT '請負',
        contract INTEGER DEFAULT 0,
        manday_price INTEGER DEFAULT 0,
        start_date TEXT DEFAULT '',
        end_date TEXT DEFAULT '',
        status TEXT DEFAULT '準備中',
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS extra_works (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        site_id TEXT NOT NULL, date TEXT NOT NULL,
        description TEXT DEFAULT '', amount INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (site_id) REFERENCES sites(id)
    );
    CREATE TABLE IF NOT EXISTS daily_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL, emp_id TEXT NOT NULL, site_id TEXT NOT NULL,
        overtime_h REAL DEFAULT 0,
        drive_type TEXT DEFAULT 'なし',
        drive_km REAL DEFAULT 0,
        memo TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS subcons (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL, vendor TEXT NOT NULL, site_id TEXT NOT NULL,
        qty REAL DEFAULT 1, unit TEXT DEFAULT '人工',
        price INTEGER DEFAULT 0, status TEXT DEFAULT '未払',
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    """)

    # ── マイグレーション ──
    emp_cols  = [r[1] for r in cur.execute("PRAGMA table_info(employees)")]
    site_cols = [r[1] for r in cur.execute("PRAGMA table_info(sites)")]
    log_cols  = [r[1] for r in cur.execute("PRAGMA table_info(daily_logs)")]

    if "daily_wage"   not in emp_cols:  cur.execute("ALTER TABLE employees ADD COLUMN daily_wage INTEGER DEFAULT 0")
    if "password"     not in emp_cols:  cur.execute("ALTER TABLE employees ADD COLUMN password TEXT DEFAULT ''")
    if "role"         not in emp_cols:  cur.execute("ALTER TABLE employees ADD COLUMN role TEXT DEFAULT 'employee'")
    if "site_type"    not in site_cols: cur.execute("ALTER TABLE sites ADD COLUMN site_type TEXT DEFAULT '請負'")
    if "manday_price" not in site_cols: cur.execute("ALTER TABLE sites ADD COLUMN manday_price INTEGER DEFAULT 0")
    if "start_date"   not in site_cols: cur.execute("ALTER TABLE sites ADD COLUMN start_date TEXT DEFAULT ''")
    if "end_date"     not in site_cols: cur.execute("ALTER TABLE sites ADD COLUMN end_date TEXT DEFAULT ''")
    if "overtime_h"   not in log_cols:  cur.execute("ALTER TABLE daily_logs ADD COLUMN overtime_h REAL DEFAULT 0")
    if "drive_type"   not in log_cols:  cur.execute("ALTER TABLE daily_logs ADD COLUMN drive_type TEXT DEFAULT 'なし'")
    if "drive_km"     not in log_cols:  cur.execute("ALTER TABLE daily_logs ADD COLUMN drive_km REAL DEFAULT 0")
    # 旧カラムがあっても無視（start_time等）

    # ── サンプルデータ ──
    if cur.execute("SELECT COUNT(*) FROM employees").fetchone()[0] == 0:
        cur.executemany("INSERT INTO employees (id,name,type,daily_wage,password,role) VALUES (?,?,?,?,?,?)", [
            ("MGR",  "㓛刀 代表",  "従業員", 0,     hash_pw("admin1234"), "manager"),
            ("E001", "田中 太郎",  "従業員", 15000, hash_pw("tanaka123"), "employee"),
            ("E002", "山田 花子",  "従業員", 14000, hash_pw("yamada123"), "employee"),
        ])
        cur.executemany("INSERT INTO sites (id,name,client,site_type,contract,manday_price,start_date,end_date,status) VALUES (?,?,?,?,?,?,?,?,?)", [
            ("S001","〇〇ビル新築工事","〇〇建設",  "請負",5000000,0,    "2025-01-10","2025-06-30","進行中"),
            ("S002","△△マンション改修","△△不動産", "請負",3000000,0,    "2025-02-01","2025-05-31","進行中"),
            ("S003","□□応援工事",      "□□建設",   "応援",0,      25000,"2025-03-01","",          "進行中"),
            ("S004","◇◇住宅リフォーム","直接受注",  "請負",1200000,0,    "2025-04-01","",          "準備中"),
        ])
        td = datetime.now().strftime("%Y-%m-%d")
        cur.executemany("INSERT INTO daily_logs (date,emp_id,site_id,overtime_h,drive_type,drive_km,memo) VALUES (?,?,?,?,?,?,?)", [
            (td,"E001","S001",0,  "なし",0,  ""),
            (td,"E002","S001",1.5,"片道",20, ""),
            (td,"MGR", "S001",0,  "往復",30, ""),
        ])
        cur.executemany("INSERT INTO subcons (date,vendor,site_id,qty,unit,price,status) VALUES (?,?,?,?,?,?,?)", [
            (td,"A社","S001",3,"人工",25000,"未払"),
            (td,"B社","S002",2,"人工",22000,"未払"),
        ])
    con.commit(); con.close()
    print(f"✅ DB初期化完了: {DB_PATH}")

def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def rows(r): return [dict(x) for x in r]
def row(r):  return dict(r) if r else None

def calc_pay(log, daily_wage):
    """1日報の給与を計算"""
    ot_pay    = round(log["overtime_h"] * OT_HOURLY)
    km        = log["drive_km"] or 0
    dt        = log["drive_type"] or "なし"
    # 片道=km、往復=km×2
    actual_km = km * 2 if dt == "往復" else km if dt == "片道" else 0
    drive_pay = round(actual_km * DRIVE_PER_KM)
    move_pay  = round(actual_km * MOVE_PER_KM)
    base      = daily_wage if daily_wage > 0 else 0
    return {
        "base": base, "ot_pay": ot_pay,
        "drive_pay": drive_pay, "move_pay": move_pay,
        "total": base + ot_pay + drive_pay + move_pay,
        "actual_km": actual_km,
    }

def calc_labor_cost(logs_with_wage):
    """日報リスト（daily_wage付き）から労務費と人工数を返す"""
    labor = 0; mandays = len(logs_with_wage)
    for l in logs_with_wage:
        if l["daily_wage"] > 0:
            p = calc_pay(l, l["daily_wage"])
            labor += p["total"]
    return labor, mandays

# ── Handler ──────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {fmt%args}")

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",len(body))
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET,POST,PUT,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Content-Type,Authorization")
        self.end_headers(); self.wfile.write(body)

    def send_file(self, path):
        with open(path,"rb") as f: data=f.read()
        self.send_response(200)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("Content-Length",len(data))
        self.end_headers(); self.wfile.write(data)

    def body(self):
        n=int(self.headers.get("Content-Length",0))
        return json.loads(self.rfile.read(n)) if n else {}

    def token(self):
        return self.headers.get("Authorization","").replace("Bearer ","").strip() or None

    def auth(self, mgr=False):
        s=get_session(self.token())
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
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/")
        qs     = parse_qs(parsed.query)

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
                self.send_json(rows(con.execute("SELECT id,name,type,daily_wage,role,created_at FROM employees ORDER BY id").fetchall()))

            elif path=="/api/sites":
                if not self.auth(): return
                self.send_json(rows(con.execute("SELECT * FROM sites ORDER BY id").fetchall()))

            elif path=="/api/logs":
                s=self.auth()
                if not s: return
                if s["role"]=="manager":
                    r=con.execute("""SELECT dl.*,e.name emp_name,e.daily_wage,st.name site_name
                        FROM daily_logs dl
                        LEFT JOIN employees e ON dl.emp_id=e.id
                        LEFT JOIN sites st ON dl.site_id=st.id
                        ORDER BY dl.date DESC,dl.id DESC""").fetchall()
                else:
                    r=con.execute("""SELECT dl.*,e.name emp_name,e.daily_wage,st.name site_name
                        FROM daily_logs dl
                        LEFT JOIN employees e ON dl.emp_id=e.id
                        LEFT JOIN sites st ON dl.site_id=st.id
                        WHERE dl.emp_id=? ORDER BY dl.date DESC,dl.id DESC""",(s["emp_id"],)).fetchall()
                self.send_json(rows(r))

            elif path=="/api/subcons":
                if not self.auth(mgr=True): return
                self.send_json(rows(con.execute("SELECT sc.*,st.name site_name FROM subcons sc LEFT JOIN sites st ON sc.site_id=st.id ORDER BY sc.date DESC,sc.id DESC").fetchall()))

            elif path=="/api/extra_works":
                if not self.auth(mgr=True): return
                sid=qs.get("site_id",[""])[0]
                if sid:
                    r=con.execute("SELECT * FROM extra_works WHERE site_id=? ORDER BY date DESC",(sid,)).fetchall()
                else:
                    r=con.execute("SELECT ew.*,st.name site_name FROM extra_works ew LEFT JOIN sites st ON ew.site_id=st.id ORDER BY ew.date DESC").fetchall()
                self.send_json(rows(r))

            elif path=="/api/summary":
                if not self.auth(mgr=True): return
                sites = rows(con.execute("SELECT * FROM sites ORDER BY id").fetchall())
                for st in sites:
                    sid = st["id"]
                    logs = rows(con.execute("""SELECT dl.*,e.daily_wage FROM daily_logs dl
                        JOIN employees e ON dl.emp_id=e.id WHERE dl.site_id=?""",(sid,)).fetchall())
                    labor, emp_mandays = calc_labor_cost(logs)

                    subcon_rows = con.execute("SELECT qty,qty*price total FROM subcons WHERE site_id=?",(sid,)).fetchall()
                    subcon_cost   = sum(r["total"] for r in subcon_rows)
                    subcon_mandays = sum(r["qty"] for r in subcon_rows)
                    total_mandays  = emp_mandays + subcon_mandays

                    if st.get("site_type") == "応援":
                        revenue    = total_mandays * st.get("manday_price", 0)
                        total_cost = labor
                    else:
                        extra   = con.execute("SELECT COALESCE(SUM(amount),0) t FROM extra_works WHERE site_id=?",(sid,)).fetchone()["t"]
                        revenue = st["contract"] + extra
                        st["extra_amount"] = extra
                        total_cost = labor + subcon_cost

                    profit = revenue - total_cost
                    cost_per_manday    = round(total_cost / total_mandays) if total_mandays > 0 else 0
                    revenue_per_manday = round(revenue   / total_mandays) if total_mandays > 0 else 0

                    st.update({
                        "revenue": revenue, "labor_cost": labor,
                        "subcon_cost": subcon_cost, "total_cost": total_cost,
                        "profit": profit,
                        "profit_rate": round(profit/revenue, 4) if revenue > 0 else 0,
                        "emp_mandays": emp_mandays,
                        "subcon_mandays": round(subcon_mandays, 1),
                        "total_mandays": round(total_mandays, 1),
                        "cost_per_manday": cost_per_manday,
                        "revenue_per_manday": revenue_per_manday,
                    })
                self.send_json(sites)

            elif path=="/api/salary":
                s=self.auth()
                if not s: return
                if s["role"]=="manager":
                    emps = rows(con.execute("SELECT * FROM employees ORDER BY id").fetchall())
                else:
                    emps = rows(con.execute("SELECT * FROM employees WHERE id=?",(s["emp_id"],)).fetchall())

                result = []
                for e in emps:
                    logs = rows(con.execute("SELECT * FROM daily_logs WHERE emp_id=?",(e["id"],)).fetchall())
                    days      = len(logs)
                    base_pay  = days * e["daily_wage"]
                    ot_total  = sum(l["overtime_h"] for l in logs)
                    ot_pay    = round(ot_total * OT_HOURLY)
                    drive_pay = 0; move_pay = 0
                    for l in logs:
                        km = l["drive_km"] or 0
                        dt = l["drive_type"] or "なし"
                        actual_km = km*2 if dt=="往復" else km if dt=="片道" else 0
                        drive_pay += round(actual_km * DRIVE_PER_KM)
                        move_pay  += round(actual_km * MOVE_PER_KM)
                    total_pay = base_pay + ot_pay + drive_pay + move_pay
                    result.append({
                        "id": e["id"], "name": e["name"], "role": e["role"],
                        "daily_wage": e["daily_wage"],
                        "days": days, "base_pay": base_pay,
                        "ot_hours": round(ot_total, 1), "ot_pay": ot_pay,
                        "drive_pay": drive_pay, "move_pay": move_pay,
                        "total_pay": total_pay,
                    })
                self.send_json(result)

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
                con.execute("INSERT INTO employees (id,name,type,daily_wage,password,role) VALUES (?,?,?,?,?,?)",
                    (b["id"],b["name"],b.get("type","従業員"),int(b.get("daily_wage",0)),
                     hash_pw(b.get("password","password1234")),b.get("role","employee")))
                con.commit()
                self.send_json(row(con.execute("SELECT id,name,type,daily_wage,role FROM employees WHERE id=?",(b["id"],)).fetchone()),201); return

            s=self.auth()
            if not s: return

            if path=="/api/sites":
                if s["role"]!="manager": self.send_json({"error":"権限なし"},403); return
                con.execute("INSERT INTO sites (id,name,client,site_type,contract,manday_price,start_date,end_date,status) VALUES (?,?,?,?,?,?,?,?,?)",
                    (b["id"],b["name"],b.get("client",""),b.get("site_type","請負"),
                     int(b.get("contract",0)),int(b.get("manday_price",0)),
                     b.get("start_date",""),b.get("end_date",""),b.get("status","準備中")))
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
                cur=con.execute("INSERT INTO daily_logs (date,emp_id,site_id,overtime_h,drive_type,drive_km,memo) VALUES (?,?,?,?,?,?,?)",
                    (b["date"],eid,b["siteId"],
                     float(b.get("overtime_h",0)),
                     b.get("drive_type","なし"),
                     float(b.get("drive_km",0)),
                     b.get("memo","")))
                con.commit()
                self.send_json(row(con.execute("SELECT * FROM daily_logs WHERE id=?",(cur.lastrowid,)).fetchone()),201)

            elif path=="/api/subcons":
                if s["role"]!="manager": self.send_json({"error":"権限なし"},403); return
                cur=con.execute("INSERT INTO subcons (date,vendor,site_id,qty,unit,price,status) VALUES (?,?,?,?,?,?,?)",
                    (b["date"],b["vendor"],b["siteId"],float(b.get("qty",1)),b.get("unit","人工"),int(b.get("price",0)),b.get("status","未払")))
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
                if parts[1]=="subcons":
                    con.execute("UPDATE subcons SET status=? WHERE id=?",(b["status"],parts[2]))
                    con.commit(); self.send_json({"ok":True})

                elif parts[1]=="sites":
                    fields=[]; vals=[]
                    for k in ["name","client","site_type","contract","manday_price","start_date","end_date","status"]:
                        if k in b:
                            fields.append(f"{k}=?")
                            vals.append(int(b[k]) if k in ["contract","manday_price"] else b[k])
                    if fields:
                        vals.append(parts[2])
                        con.execute(f"UPDATE sites SET {','.join(fields)} WHERE id=?",vals)
                    con.commit()
                    self.send_json(row(con.execute("SELECT * FROM sites WHERE id=?",(parts[2],)).fetchone()))

                elif parts[1]=="employees":
                    fields=[]; vals=[]
                    if "password"   in b: fields.append("password=?");   vals.append(hash_pw(b["password"]))
                    if "daily_wage" in b: fields.append("daily_wage=?"); vals.append(int(b["daily_wage"]))
                    if "name"       in b: fields.append("name=?");       vals.append(b["name"])
                    if fields:
                        vals.append(parts[2])
                        con.execute(f"UPDATE employees SET {','.join(fields)} WHERE id=?",vals)
                    con.commit()
                    self.send_json(row(con.execute("SELECT id,name,type,daily_wage,role FROM employees WHERE id=?",(parts[2],)).fetchone()))

                else: self.send_json({"error":"Not found"},404)
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
                tmap={"employees":"employees","sites":"sites","logs":"daily_logs",
                      "subcons":"subcons","extra_works":"extra_works"}
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
初期ログイン: MGR/admin1234  E001/tanaka123  E002/yamada123
""")
    try: server.serve_forever()
    except KeyboardInterrupt: print("\n停止"); server.server_close()
