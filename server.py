#!/usr/bin/env python3
"""㓛刀工業 作業管理システム"""

import sqlite3, json, os, hashlib, secrets, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime

PORT    = int(os.environ.get("PORT", 8000))
DB_PATH = "/data/kukito.db"

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

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS employees (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT DEFAULT '従業員',
        daily_wage INTEGER DEFAULT 0,
        trip_allowance INTEGER DEFAULT 0,
        password TEXT DEFAULT '', role TEXT DEFAULT 'employee',
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS employee_site_km (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_id TEXT NOT NULL, site_id TEXT NOT NULL,
        one_way_km REAL DEFAULT 0,
        UNIQUE(emp_id, site_id),
        FOREIGN KEY (emp_id) REFERENCES employees(id),
        FOREIGN KEY (site_id) REFERENCES sites(id)
    );
    CREATE TABLE IF NOT EXISTS sites (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, client TEXT DEFAULT '',
        site_type TEXT DEFAULT '請負',
        contract INTEGER DEFAULT 0,
        manday_price INTEGER DEFAULT 0,
        one_way_km REAL DEFAULT 0,
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
        is_trip INTEGER DEFAULT 0,
        memo TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS companies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        zip_code TEXT DEFAULT '',
        address TEXT DEFAULT '',
        tel TEXT DEFAULT '',
        fax TEXT DEFAULT '',
        contact TEXT DEFAULT '',
        note TEXT DEFAULT '',
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
    tables    = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]

    if "daily_wage"     not in emp_cols:  cur.execute("ALTER TABLE employees ADD COLUMN daily_wage INTEGER DEFAULT 0")
    if "trip_allowance" not in emp_cols:  cur.execute("ALTER TABLE employees ADD COLUMN trip_allowance INTEGER DEFAULT 0")
    if "password"       not in emp_cols:  cur.execute("ALTER TABLE employees ADD COLUMN password TEXT DEFAULT ''")
    if "role"           not in emp_cols:  cur.execute("ALTER TABLE employees ADD COLUMN role TEXT DEFAULT 'employee'")
    if "site_type"         not in site_cols: cur.execute("ALTER TABLE sites ADD COLUMN site_type TEXT DEFAULT '請負'")
    if "support_company_id" not in site_cols: cur.execute("ALTER TABLE sites ADD COLUMN support_company_id INTEGER DEFAULT NULL")
    comp_cols = [r[1] for r in cur.execute("PRAGMA table_info(companies)")]
    if "zip_code" not in comp_cols: cur.execute("ALTER TABLE companies ADD COLUMN zip_code TEXT DEFAULT ''")
    if "one_way_km"     not in site_cols: cur.execute("ALTER TABLE sites ADD COLUMN one_way_km REAL DEFAULT 0")
    if "manday_price"   not in site_cols: cur.execute("ALTER TABLE sites ADD COLUMN manday_price INTEGER DEFAULT 0")
    if "start_date"     not in site_cols: cur.execute("ALTER TABLE sites ADD COLUMN start_date TEXT DEFAULT ''")
    if "end_date"       not in site_cols: cur.execute("ALTER TABLE sites ADD COLUMN end_date TEXT DEFAULT ''")
    if "overtime_h"     not in log_cols:  cur.execute("ALTER TABLE daily_logs ADD COLUMN overtime_h REAL DEFAULT 0")
    if "drive_type"     not in log_cols:  cur.execute("ALTER TABLE daily_logs ADD COLUMN drive_type TEXT DEFAULT 'なし'")
    if "drive_km"       not in log_cols:  cur.execute("ALTER TABLE daily_logs ADD COLUMN drive_km REAL DEFAULT 0")
    if "is_trip"        not in log_cols:  cur.execute("ALTER TABLE daily_logs ADD COLUMN is_trip INTEGER DEFAULT 0")
    if "is_move"        not in log_cols:  cur.execute("ALTER TABLE daily_logs ADD COLUMN is_move INTEGER DEFAULT 0")
    if "move_type"      not in log_cols:  cur.execute("ALTER TABLE daily_logs ADD COLUMN move_type TEXT DEFAULT 'なし'")
    if "employee_site_km" not in tables:
        cur.execute("""CREATE TABLE employee_site_km (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            emp_id TEXT NOT NULL, site_id TEXT NOT NULL,
            one_way_km REAL DEFAULT 0,
            UNIQUE(emp_id, site_id))""")

    # ── サンプルデータ ──
    if cur.execute("SELECT COUNT(*) FROM employees").fetchone()[0] == 0:
        cur.executemany("INSERT INTO employees (id,name,type,daily_wage,trip_allowance,password,role) VALUES (?,?,?,?,?,?,?)", [
            ("MGR",  "㓛刀 代表",  "従業員", 0,     0,    hash_pw("admin1234"), "manager"),
            ("E001", "田中 太郎",  "従業員", 15000, 3000, hash_pw("tanaka123"), "employee"),
            ("E002", "山田 花子",  "従業員", 14000, 3000, hash_pw("yamada123"), "employee"),
        ])
        cur.executemany("INSERT INTO sites (id,name,client,site_type,contract,manday_price,one_way_km,start_date,end_date,status) VALUES (?,?,?,?,?,?,?,?,?,?)", [
            ("S001","〇〇ビル新築工事","〇〇建設",  "請負",5000000,0,    20,"2025-01-10","2025-06-30","進行中"),
            ("S002","△△マンション改修","△△不動産", "請負",3000000,0,    35,"2025-02-01","2025-05-31","進行中"),
            ("S003","□□応援工事",      "□□建設",   "応援",0,      25000,0,"2025-03-01","",          "進行中"),
        ])
        cur.executemany("INSERT INTO employee_site_km (emp_id,site_id,one_way_km) VALUES (?,?,?)", [
            ("E001","S001",20), ("E001","S002",35),
            ("E002","S001",30), ("E002","S002",28),
        ])
        td = datetime.now().strftime("%Y-%m-%d")
        cur.executemany("INSERT INTO daily_logs (date,emp_id,site_id,overtime_h,drive_type,drive_km,is_trip,memo) VALUES (?,?,?,?,?,?,?,?)", [
            (td,"E001","S001",0,  "往復",20,0,""),
            (td,"E002","S001",1.5,"往復",30,1,"出張あり"),
            (td,"MGR", "S001",0,  "なし",0, 0,""),
        ])
        cur.executemany("INSERT INTO subcons (date,vendor,site_id,qty,unit,price,status) VALUES (?,?,?,?,?,?,?)", [
            (td,"A社","S001",3,"人工",25000,"未払"),
        ])
    # companies テーブルのサンプルは挿入しない（実データ保護）
    con.commit(); con.close()
    print(f"✅ DB初期化完了: {DB_PATH}")

def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def rows(r): return [dict(x) for x in r]
def row(r):  return dict(r) if r else None

def calc_pay(log, daily_wage, trip_allowance):
    """1日報の給与計算
    運転（片道/往復）: 運転手当（4円/km）のみ
    移動あり(is_move=1): 移動手当（14円/km）× 片道km
    """
    km = log.get("drive_km") or 0
    dt = log.get("drive_type") or "なし"
    driven_km = km * 2 if dt == "往復" else km if dt == "片道" else 0
    mt = log.get("move_type") or ("片道" if log.get("is_move") else "なし")
    move_km   = km * 2 if mt == "往復" else km if mt == "片道" else 0
    ot_pay    = round((log.get("overtime_h") or 0) * OT_HOURLY)
    drive_pay = round(driven_km * DRIVE_PER_KM)
    move_pay  = round(move_km   * MOVE_PER_KM)
    trip_pay  = trip_allowance if log.get("is_trip") else 0
    base      = daily_wage if daily_wage > 0 else 0
    return {
        "base": base, "ot_pay": ot_pay,
        "drive_pay": drive_pay, "move_pay": move_pay,
        "trip_pay": trip_pay,
        "actual_km": driven_km, "move_km": move_km,
        "total": base + ot_pay + drive_pay + move_pay + trip_pay,
    }

def calc_labor_cost(logs_with_wage):
    labor = 0; mandays = len(logs_with_wage)
    for l in logs_with_wage:
        if (l.get("daily_wage") or 0) > 0:
            p = calc_pay(l, l["daily_wage"], l.get("trip_allowance", 0))
            labor += p["total"]
    return labor, mandays

def build_salary(emp, logs):
    """給与サマリー構築"""
    days = len(logs)
    base_pay = days * emp["daily_wage"]
    ot_h = sum(l.get("overtime_h") or 0 for l in logs)
    ot_pay = round(ot_h * OT_HOURLY)
    trip_days = sum(1 for l in logs if l.get("is_trip"))
    trip_pay  = trip_days * emp["trip_allowance"]
    drive_pay = move_pay = 0
    for l in logs:
        km = l.get("drive_km") or 0
        dt = l.get("drive_type") or "なし"
        driven_km = km*2 if dt=="往復" else km if dt=="片道" else 0
        mt2 = l.get("move_type") or ("片道" if l.get("is_move") else "なし")
        move_km   = km*2 if mt2=="往復" else km if mt2=="片道" else 0
        drive_pay += round(driven_km * DRIVE_PER_KM)
        move_pay  += round(move_km   * MOVE_PER_KM)
    total_pay = base_pay + ot_pay + trip_pay + drive_pay + move_pay
    return {
        "id": emp["id"], "name": emp["name"], "role": emp.get("role","employee"),
        "daily_wage": emp["daily_wage"], "trip_allowance": emp["trip_allowance"],
        "days": days, "base_pay": base_pay,
        "ot_hours": round(ot_h, 1), "ot_pay": ot_pay,
        "trip_days": trip_days, "trip_pay": trip_pay,
        "drive_pay": drive_pay, "move_pay": move_pay,
        "total_pay": total_pay,
    }

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
                self.send_json(rows(con.execute("SELECT id,name,type,daily_wage,trip_allowance,role,created_at FROM employees ORDER BY id").fetchall()))

            elif path=="/api/sites":
                if not self.auth(): return
                r=con.execute("""SELECT s.*,c.name support_company_name
                    FROM sites s LEFT JOIN companies c ON s.support_company_id=c.id
                    ORDER BY s.id""").fetchall()
                self.send_json(rows(r))

            elif path=="/api/emp_site_km":
                s=self.auth()
                if not s: return
                if s["role"]=="manager":
                    r=con.execute("""SELECT esk.*,e.name emp_name,st.name site_name
                        FROM employee_site_km esk
                        LEFT JOIN employees e ON esk.emp_id=e.id
                        LEFT JOIN sites st ON esk.site_id=st.id
                        ORDER BY esk.emp_id,esk.site_id""").fetchall()
                else:
                    r=con.execute("""SELECT esk.*,st.name site_name
                        FROM employee_site_km esk
                        LEFT JOIN sites st ON esk.site_id=st.id
                        WHERE esk.emp_id=? ORDER BY esk.site_id""",(s["emp_id"],)).fetchall()
                self.send_json(rows(r))

            elif path=="/api/logs":
                s=self.auth()
                if not s: return
                if s["role"]=="manager":
                    r=con.execute("""SELECT dl.*,e.name emp_name,e.daily_wage,e.trip_allowance,st.name site_name
                        FROM daily_logs dl
                        LEFT JOIN employees e ON dl.emp_id=e.id
                        LEFT JOIN sites st ON dl.site_id=st.id
                        ORDER BY dl.date DESC,dl.id DESC""").fetchall()
                else:
                    r=con.execute("""SELECT dl.*,e.name emp_name,e.daily_wage,e.trip_allowance,st.name site_name
                        FROM daily_logs dl
                        LEFT JOIN employees e ON dl.emp_id=e.id
                        LEFT JOIN sites st ON dl.site_id=st.id
                        WHERE dl.emp_id=? ORDER BY dl.date DESC,dl.id DESC""",(s["emp_id"],)).fetchall()
                self.send_json(rows(r))

            elif path=="/api/subcons":
                if not self.auth(mgr=True): return
                self.send_json(rows(con.execute("SELECT sc.*,st.name site_name FROM subcons sc LEFT JOIN sites st ON sc.site_id=st.id ORDER BY sc.date DESC,sc.id DESC").fetchall()))

            elif path=="/api/companies":
                if not self.auth(): return
                self.send_json(rows(con.execute("SELECT * FROM companies ORDER BY name").fetchall()))

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
                    logs = rows(con.execute("""SELECT dl.*,e.daily_wage,e.trip_allowance FROM daily_logs dl
                        JOIN employees e ON dl.emp_id=e.id WHERE dl.site_id=?""",(sid,)).fetchall())
                    labor, emp_mandays = calc_labor_cost(logs)
                    subcon_rows = con.execute("SELECT qty,qty*price total FROM subcons WHERE site_id=?",(sid,)).fetchall()
                    subcon_cost    = sum(r["total"] for r in subcon_rows)
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
                    st.update({
                        "revenue": revenue, "labor_cost": labor,
                        "subcon_cost": subcon_cost, "total_cost": total_cost,
                        "profit": profit,
                        "profit_rate": round(profit/revenue, 4) if revenue > 0 else 0,
                        "emp_mandays": emp_mandays,
                        "subcon_mandays": round(subcon_mandays, 1),
                        "total_mandays": round(total_mandays, 1),
                        "cost_per_manday": round(total_cost/total_mandays) if total_mandays > 0 else 0,
                        "revenue_per_manday": round(revenue/total_mandays) if total_mandays > 0 else 0,
                    })
                self.send_json(sites)

            elif path=="/api/salary":
                s=self.auth()
                if not s: return
                # year/month フィルター
                year  = qs.get("year",  [""])[0]
                month = qs.get("month", [""])[0]
                def date_filter(emp_id):
                    conds=["emp_id=?"]; vals=[emp_id]
                    if year:  conds.append("substr(date,1,4)=?"); vals.append(year)
                    if month: conds.append("substr(date,6,2)=?"); vals.append(month.zfill(2))
                    return " AND ".join(conds), vals

                if s["role"]=="manager":
                    emps = rows(con.execute("SELECT * FROM employees ORDER BY id").fetchall())
                else:
                    emps = rows(con.execute("SELECT * FROM employees WHERE id=?",(s["emp_id"],)).fetchall())

                result=[]
                for e in emps:
                    cond, vals = date_filter(e["id"])
                    logs = rows(con.execute(f"SELECT * FROM daily_logs WHERE {cond} ORDER BY date",(vals)).fetchall())
                    result.append(build_salary(e, logs))
                self.send_json(result)

            elif path=="/api/salary_detail":
                # 給与明細用：特定従業員・年月の日報一覧
                s=self.auth()
                if not s: return
                emp_id = qs.get("emp_id",[""])[0] or s["emp_id"]
                if s["role"]!="manager" and emp_id!=s["emp_id"]:
                    self.send_json({"error":"権限なし"},403); return
                year  = qs.get("year",  [""])[0]
                month = qs.get("month", [""])[0]
                conds=["dl.emp_id=?"]; vals=[emp_id]
                if year:  conds.append("substr(dl.date,1,4)=?"); vals.append(year)
                if month: conds.append("substr(dl.date,6,2)=?"); vals.append(month.zfill(2))
                logs = rows(con.execute(f"""SELECT dl.*,st.name site_name FROM daily_logs dl
                    LEFT JOIN sites st ON dl.site_id=st.id
                    WHERE {' AND '.join(conds)} ORDER BY dl.date""",(vals)).fetchall())
                emp  = row(con.execute("SELECT * FROM employees WHERE id=?",(emp_id,)).fetchone())
                self.send_json({"emp": emp, "logs": logs})

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
                con.execute("INSERT INTO employees (id,name,type,daily_wage,trip_allowance,password,role) VALUES (?,?,?,?,?,?,?)",
                    (b["id"],b["name"],b.get("type","従業員"),int(b.get("daily_wage",0)),
                     int(b.get("trip_allowance",0)),hash_pw(b.get("password","password1234")),b.get("role","employee")))
                con.commit()
                self.send_json(row(con.execute("SELECT id,name,type,daily_wage,trip_allowance,role FROM employees WHERE id=?",(b["id"],)).fetchone()),201); return

            s=self.auth()
            if not s: return

            if path=="/api/emp_site_km":
                # 従業員×現場のkm設定（代表のみ）
                if s["role"]!="manager": self.send_json({"error":"権限なし"},403); return
                con.execute("""INSERT INTO employee_site_km (emp_id,site_id,one_way_km) VALUES (?,?,?)
                    ON CONFLICT(emp_id,site_id) DO UPDATE SET one_way_km=excluded.one_way_km""",
                    (b["empId"],b["siteId"],float(b.get("km",0))))
                con.commit()
                self.send_json({"ok":True})

            elif path=="/api/sites":
                if s["role"]!="manager": self.send_json({"error":"権限なし"},403); return
                con.execute("INSERT INTO sites (id,name,client,site_type,contract,manday_price,one_way_km,support_company_id,start_date,end_date,status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (b["id"],b["name"],b.get("client",""),b.get("site_type","請負"),
                     int(b.get("contract",0)),int(b.get("manday_price",0)),
                     float(b.get("one_way_km",0)),
                     int(b["support_company_id"]) if b.get("support_company_id") else None,
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
                # km は sites テーブルから自動取得（運転なしでも移動手当のためにkmを保存）
                drive_km = float(b.get("drive_km", 0))
                if drive_km == 0:
                    r = con.execute("SELECT one_way_km FROM sites WHERE id=?", (b["siteId"],)).fetchone()
                    if r: drive_km = r["one_way_km"]
                cur=con.execute("INSERT INTO daily_logs (date,emp_id,site_id,overtime_h,drive_type,drive_km,is_trip,move_type,memo) VALUES (?,?,?,?,?,?,?,?,?)",
                    (b["date"],eid,b["siteId"],
                     float(b.get("overtime_h",0)),
                     b.get("drive_type","なし"),
                     drive_km,
                     1 if b.get("is_trip") else 0,
                     b.get("move_type","なし"),
                     b.get("memo","")))
                con.commit()
                self.send_json(row(con.execute("SELECT * FROM daily_logs WHERE id=?",(cur.lastrowid,)).fetchone()),201)

            elif path=="/api/companies":
                if s["role"]!="manager": self.send_json({"error":"権限なし"},403); return
                cur=con.execute("INSERT INTO companies (name,zip_code,address,tel,fax,contact,note) VALUES (?,?,?,?,?,?,?)",
                    (b["name"],b.get("zip_code",""),b.get("address",""),b.get("tel",""),b.get("fax",""),b.get("contact",""),b.get("note","")))
                con.commit()
                self.send_json(row(con.execute("SELECT * FROM companies WHERE id=?",(cur.lastrowid,)).fetchone()),201)

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
                    old_id = parts[2]
                    new_id = b.get("new_id", "").strip()
                    # IDが変更される場合：新IDで作り直し→関連データ付け替え→旧ID削除
                    if new_id and new_id != old_id:
                        if con.execute("SELECT id FROM sites WHERE id=?",(new_id,)).fetchone():
                            self.send_json({"error":f"ID '{new_id}' は既に使用されています"},409); return
                        # 現在のデータを取得してnew_idで挿入
                        orig = row(con.execute("SELECT * FROM sites WHERE id=?",(old_id,)).fetchone())
                        if not orig: self.send_json({"error":"現場が見つかりません"},404); return
                        # 更新フィールドをorigに上書き
                        for k in ["name","client","site_type","contract","manday_price","one_way_km","support_company_id","start_date","end_date","status"]:
                            if k in b: orig[k] = float(b[k]) if k=="one_way_km" else int(b[k]) if k in ["contract","manday_price"] else (int(b[k]) if k=="support_company_id" and b[k] else None if k=="support_company_id" else b[k])
                        con.execute("INSERT INTO sites (id,name,client,site_type,contract,manday_price,one_way_km,support_company_id,start_date,end_date,status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (new_id,orig["name"],orig.get("client",""),orig.get("site_type","請負"),
                             orig.get("contract",0),orig.get("manday_price",0),orig.get("one_way_km",0),
                             orig.get("support_company_id"),orig.get("start_date",""),orig.get("end_date",""),orig.get("status","準備中")))
                        # 関連データを付け替え
                        con.execute("UPDATE daily_logs SET site_id=? WHERE site_id=?",(new_id,old_id))
                        con.execute("UPDATE subcons SET site_id=? WHERE site_id=?",(new_id,old_id))
                        con.execute("UPDATE extra_works SET site_id=? WHERE site_id=?",(new_id,old_id))
                        con.execute("DELETE FROM sites WHERE id=?",(old_id,))
                        con.commit()
                        self.send_json(row(con.execute("SELECT * FROM sites WHERE id=?",(new_id,)).fetchone()))
                    else:
                        # IDは変わらない：通常の UPDATE
                        fields=[]; vals=[]
                        for k in ["name","client","site_type","contract","manday_price","one_way_km","support_company_id","start_date","end_date","status"]:
                            if k in b:
                                fields.append(f"{k}=?")
                                vals.append(float(b[k]) if k in ["one_way_km"] else int(b[k]) if k in ["contract","manday_price"] else (int(b[k]) if k=="support_company_id" and b[k] else None if k=="support_company_id" else b[k]))
                        if fields:
                            vals.append(old_id)
                            con.execute(f"UPDATE sites SET {','.join(fields)} WHERE id=?",vals)
                        con.commit()
                        self.send_json(row(con.execute("SELECT * FROM sites WHERE id=?",(old_id,)).fetchone()))

                elif parts[1]=="logs":
                    fields=[]; vals=[]
                    for k in ["date","emp_id","site_id","overtime_h","drive_type","is_trip","move_type","memo"]:
                        if k in b:
                            fields.append(f"{k}=?")
                            vals.append(float(b[k]) if k=="overtime_h" else int(b[k]) if k=="is_trip" else b[k])
                    # drive_km は site から再取得
                    if "site_id" in b or "drive_type" in b:
                        sid = b.get("site_id") or con.execute("SELECT site_id FROM daily_logs WHERE id=?",(parts[2],)).fetchone()["site_id"]
                        dt  = b.get("drive_type") or con.execute("SELECT drive_type FROM daily_logs WHERE id=?",(parts[2],)).fetchone()["drive_type"]
                        r   = con.execute("SELECT one_way_km FROM sites WHERE id=?",(sid,)).fetchone()
                        km  = r["one_way_km"] if r else 0
                        fields.append("drive_km=?"); vals.append(km)
                        fields.append("site_id=?");  vals.append(sid)
                    if fields:
                        # 重複除去
                        seen={}
                        for f,v in zip(fields,vals): seen[f]=v
                        vals2=list(seen.values())+[parts[2]]
                        con.execute(f"UPDATE daily_logs SET {','.join(seen.keys())} WHERE id=?",vals2)
                    con.commit()
                    self.send_json(row(con.execute("SELECT * FROM daily_logs WHERE id=?",(parts[2],)).fetchone()))

                elif parts[1]=="companies":
                    fields=[]; vals=[]
                    for k in ["name","zip_code","address","tel","fax","contact","note"]:
                        if k in b: fields.append(f"{k}=?"); vals.append(b[k])
                    if fields:
                        vals.append(parts[2])
                        con.execute(f"UPDATE companies SET {','.join(fields)} WHERE id=?",vals)
                    con.commit()
                    self.send_json(row(con.execute("SELECT * FROM companies WHERE id=?",(parts[2],)).fetchone()))

                elif parts[1]=="employees":
                    fields=[]; vals=[]
                    if "password"       in b: fields.append("password=?");       vals.append(hash_pw(b["password"]))
                    if "daily_wage"     in b: fields.append("daily_wage=?");     vals.append(int(b["daily_wage"]))
                    if "trip_allowance" in b: fields.append("trip_allowance=?"); vals.append(int(b["trip_allowance"]))
                    if "name"           in b: fields.append("name=?");           vals.append(b["name"])
                    if fields:
                        vals.append(parts[2])
                        con.execute(f"UPDATE employees SET {','.join(fields)} WHERE id=?",vals)
                    con.commit()
                    self.send_json(row(con.execute("SELECT id,name,type,daily_wage,trip_allowance,role FROM employees WHERE id=?",(parts[2],)).fetchone()))

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
                      "subcons":"subcons","extra_works":"extra_works","emp_site_km":"employee_site_km",
                      "companies":"companies"}
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
