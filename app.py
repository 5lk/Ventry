import os
from functools import wraps
from flask import Flask, request, redirect, render_template, session, url_for, flash
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler

from models import init_db, db
from services.algorand import (
    generate_wallet, fund_account, create_asa, ensure_opt_in,
    deploy_price_app, update_token_price, get_app_state, atomic_approve_and_pay,
    SCALE_GBP
)
from algosdk import mnemonic as algo_mnemonic
from services.valuation import compute_token_price_scaled_gbp

def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev_secret")
    init_db()

    def login_required(role=None):
        def deco(f):
            @wraps(f)
            def inner(*args, **kwargs):
                if "user_id" not in session:
                    return redirect(url_for("login"))
                if role and session.get("role") != role:
                    flash("Unauthorized")
                    return redirect(url_for("dashboard"))
                return f(*args, **kwargs)
            return inner
        return deco

    @app.context_processor
    def inject_globals():
        return dict(SCALE_GBP=SCALE_GBP, APP_NAME="Ventry")

    @app.template_global()
    def format_gbp_pence(pence: int | None):
        if pence is None:
            return "£0.00"
        return f"£{pence/100:.2f}"

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/register", methods=["GET","POST"])
    def register():
        if request.method == "POST":
            email = request.form["email"].strip().lower()
            password = request.form["password"]
            role = request.form["role"]
            first_name = request.form.get("first_name","").strip()
            last_name  = request.form.get("last_name","").strip()
            home_address = request.form.get("home_address","").strip()
            linkedin_url = request.form.get("linkedin_url","").strip() if role == "developer" else None

            sk, addr, mnem = generate_wallet()
            conn = db()
            try:
                conn.execute(
                    "INSERT INTO users(email,password_hash,role,first_name,last_name,home_address,linkedin_url,algo_addr,algo_mnemonic) VALUES(?,?,?,?,?,?,?,?,?)",
                    (email, generate_password_hash(password), role, first_name, last_name, home_address, linkedin_url, addr, mnem)
                )
                conn.commit()
                try:
                    fund_account(addr, 10_000_000)
                except Exception as fe:
                    print("Funding warning:", fe)

                if role == "company":
                    uid = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]
                    existing = conn.execute("SELECT id FROM companies WHERE user_id=?", (uid,)).fetchone()
                    if not existing:
                        conn.execute("""
                            INSERT INTO companies(user_id,name,asset_id,app_id,unit_name,asset_name,supply,equity_pct,valuation_gbp)
                            VALUES(?,?,?,?,?,?,?,?,?)
                        """, (uid, "", None, None, None, None, None, 0.15, 1_000_000.00))
                        conn.commit()

                flash("Registered. Please sign in.")
                return redirect(url_for("login"))
            except Exception as e:
                print("Registration error:", e)
                if "UNIQUE constraint failed: users.email" in str(e):
                    flash("Registration failed: email already registered.")
                else:
                    flash(f"Registration failed: {e}")
            finally:
                conn.close()
        return render_template("register.html")

    @app.route("/login", methods=["GET","POST"])
    def login():
        if request.method == "POST":
            email = request.form["email"].strip().lower()
            password = request.form["password"]
            conn = db()
            user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            conn.close()
            if user and check_password_hash(user["password_hash"], password):
                session["user_id"] = user["id"]
                session["role"]   = user["role"]
                if user["role"] == "company":
                    conn2 = db()
                    comp = conn2.execute("SELECT * FROM companies WHERE user_id=?", (user["id"],)).fetchone()
                    conn2.close()
                    if comp and (not comp["name"] or comp["asset_id"] is None or comp["app_id"] is None):
                        return redirect(url_for("company_setup"))
                return redirect(url_for("dashboard"))
            flash("Invalid credentials.")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("index"))

    @app.route("/dashboard")
    def dashboard():
        if "user_id" not in session:
            return redirect(url_for("login"))
        conn = db()
        user = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
        if session["role"] == "company":
            comp = conn.execute("SELECT * FROM companies WHERE user_id=?", (user["id"],)).fetchone()
            if comp and (not comp["name"] or comp["asset_id"] is None or comp["app_id"] is None):
                conn.close()
                return redirect(url_for("company_setup"))
            jobs = conn.execute(
                "SELECT * FROM jobs WHERE company_id=? ORDER BY created_at DESC",
                (comp["id"],)
            ).fetchall() if comp and comp["name"] else []
            state = {}
            if comp and comp["app_id"]:
                try:
                    state = get_app_state(comp["app_id"])
                    if "token_price" in state:
                        state["token_price_human_gbp"] = state["token_price"]/SCALE_GBP
                except Exception:
                    state = {}
            conn.close()
            return render_template("company_dashboard.html", user=user, company=comp, jobs=jobs, state=state)
        else:
            holdings = conn.execute("""
                SELECT dh.*, c.name AS company_name, c.app_id
                FROM developer_holdings dh
                JOIN companies c ON c.id = dh.company_id
                WHERE dh.developer_id=?
            """, (user["id"],)).fetchall()
            holdings_view = []
            for h in holdings:
                price_scaled = 0
                if h["app_id"]:
                    try:
                        st = get_app_state(h["app_id"])
                        price_scaled = st.get("token_price", 0)
                    except Exception:
                        price_scaled = 0
                value_gbp = (price_scaled / SCALE_GBP) * h["tokens_held"]
                holdings_view.append(dict(
                    company_name=h["company_name"],
                    tokens=h["tokens_held"],
                    price_gbp=price_scaled / SCALE_GBP,
                    value_gbp=value_gbp
                ))
            open_jobs = conn.execute("""
                SELECT j.*, c.name AS company_name, c.supply AS company_supply
                FROM jobs j JOIN companies c ON j.company_id=c.id
                WHERE j.status='open'
                ORDER BY j.created_at DESC
            """).fetchall()
            my_current = conn.execute("""
                SELECT j.*, c.name AS company_name, c.supply AS company_supply
                FROM jobs j JOIN companies c ON j.company_id=c.id
                WHERE j.developer_id=? AND j.status IN ('picked','awaiting_verification')
                ORDER BY j.created_at DESC
            """, (user["id"],)).fetchall()
            conn.close()
            return render_template(
                "developer_dashboard.html",
                user=user,
                open_jobs=open_jobs,
                my_current=my_current,
                holdings=holdings_view
            )

    @app.route("/company/setup", methods=["GET","POST"])
    @login_required(role="company")
    def company_setup():
        conn = db()
        user = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
        comp = conn.execute("SELECT * FROM companies WHERE user_id=?", (user["id"],)).fetchone()

        if comp and comp["name"] and comp["asset_id"] is not None and comp["app_id"] is not None:
            conn.close()
            flash("Company setup already completed and locked.")
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            name   = request.form["name"].strip()
            supply = int(request.form["supply"])
            equity = float(request.form.get("equity_pct", "15"))/100.0
            valuation_gbp = float(request.form.get("valuation_gbp", "1000000"))

            unit_name = ''.join(filter(str.isalpha, name.upper()))[:5]
            asset_name = f"{name} Token"

            company_sk = algo_mnemonic.to_private_key(user["algo_mnemonic"])

            asset_id = create_asa(company_sk, unit_name, asset_name, supply, decimals=0)
            initial_price_scaled = compute_token_price_scaled_gbp(name, supply, equity, valuation_override_gbp=valuation_gbp)
            app_id = deploy_price_app(company_sk, asset_id, initial_price_scaled)

            conn.execute("""
                UPDATE companies
                SET name=?, asset_id=?, app_id=?, unit_name=?, asset_name=?, supply=?, equity_pct=?, valuation_gbp=?
                WHERE id=?
            """, (name, asset_id, app_id, unit_name, asset_name, supply, equity, valuation_gbp, comp["id"]))
            conn.commit()
            conn.close()
            flash("Company setup completed. Configuration is now locked.")
            return redirect(url_for("dashboard"))
        conn.close()
        return render_template("company_setup.html", onboarding=False, company=comp)

    @app.route("/company/jobs/new", methods=["GET","POST"])
    @login_required(role="company")
    def new_job():
        conn = db()
        comp = conn.execute("SELECT * FROM companies WHERE user_id=?", (session["user_id"],)).fetchone()
        if not comp or comp["asset_id"] is None or comp["app_id"] is None or not comp["name"]:
            conn.close()
            flash("Complete company setup first.")
            return redirect(url_for("company_setup"))
        if request.method == "POST":
            title = request.form["title"].strip()
            desc  = request.form["description"].strip()
            upfront_gbp_pence = int(round(float(request.form["upfront_gbp"]) * 100))
            token_amount  = int(request.form["token_amount"])
            conn.execute("""
                INSERT INTO jobs(company_id,title,description,upfront_gbp_pence,token_amount)
                VALUES(?,?,?,?,?)
            """, (comp["id"], title, desc, upfront_gbp_pence, token_amount))
            conn.commit()
            conn.close()
            flash("Task created.")
            return redirect(url_for("dashboard"))
        conn.close()
        return render_template("new_job.html")

    @app.route("/jobs")
    @login_required(role="developer")
    def browse_jobs():
        conn = db()
        jobs = conn.execute("""
            SELECT j.*, c.name AS company_name
            FROM jobs j JOIN companies c ON j.company_id=c.id
            WHERE j.status='open' ORDER BY j.created_at DESC
        """).fetchall()
        conn.close()
        return render_template("jobs.html", jobs=jobs)

    @app.route("/jobs/<int:job_id>")
    @login_required(role="developer")
    def view_job(job_id):
        conn = db()
        job = conn.execute("""
            SELECT j.*, c.name AS company_name, c.supply AS company_supply, c.equity_pct AS equity_pct
            FROM jobs j JOIN companies c ON j.company_id=c.id WHERE j.id=?
        """, (job_id,)).fetchone()
        conn.close()
        if not job:
            flash("Task not found.")
            return redirect(url_for("browse_jobs"))
        return render_template("job_detail.html", job=job)

    @app.route("/jobs/<int:job_id>/pickup", methods=["POST"])
    @login_required(role="developer")
    def pickup_job(job_id):
        conn = db()
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job or job["status"] != "open":
            conn.close()
            flash("Task not available to pick up.")
            return redirect(url_for("dashboard"))
        conn.execute("UPDATE jobs SET developer_id=?, status='picked' WHERE id=?", (session["user_id"], job_id))
        conn.commit()
        conn.close()
        flash("Task added to your current list.")
        return redirect(url_for("dashboard"))

    @app.route("/jobs/<int:job_id>/complete", methods=["POST"])
    @login_required(role="developer")
    def complete_job(job_id):
        conn = db()
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job or job["developer_id"] != session["user_id"] or job["status"] != "picked":
            conn.close()
            flash("Cannot mark this task as completed.")
            return redirect(url_for("dashboard"))
        conn.execute("UPDATE jobs SET developer_marked_complete=1, status='awaiting_verification' WHERE id=?", (job_id,))
        conn.commit()
        conn.close()
        flash("Task marked completed. Awaiting company verification.")
        return redirect(url_for("dashboard"))

    @app.route("/company/jobs/<int:job_id>/verify", methods=["POST"])
    @login_required(role="company")
    def verify_job(job_id):
        conn = db()
        company_user = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
        comp = conn.execute("SELECT * FROM companies WHERE user_id=?", (company_user["id"],)).fetchone()
        job  = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        dev  = conn.execute("SELECT * FROM users WHERE id=?", (job["developer_id"],)).fetchone()
        if not (comp and job and dev) or job["status"] != "awaiting_verification":
            conn.close()
            flash("Invalid task state for verification.")
            return redirect(url_for("dashboard"))

        company_sk   = algo_mnemonic.to_private_key(company_user["algo_mnemonic"])
        company_addr = company_user["algo_addr"]
        dev_addr     = dev["algo_addr"]

        try:
            ensure_opt_in(algo_mnemonic.to_private_key(dev["algo_mnemonic"]), comp["asset_id"])
        except Exception:
            pass

        new_price_scaled = compute_token_price_scaled_gbp(
            comp["name"], comp["supply"], comp["equity_pct"], valuation_override_gbp=comp["valuation_gbp"]
        )
        upfront_microalgos = 500_000 

        try:
            atomic_approve_and_pay(
                company_sk, company_addr, dev_addr, comp["app_id"], comp["asset_id"],
                upfront_microalgos, job["token_amount"], new_price_scaled
            )
        except Exception as e:
            conn.close()
            flash(f"Settlement failed: {e}")
            return redirect(url_for("dashboard"))

        existing = conn.execute("""
            SELECT * FROM developer_holdings WHERE developer_id=? AND company_id=? AND asset_id=?
        """, (dev["id"], comp["id"], comp["asset_id"])).fetchone()
        if existing:
            conn.execute("UPDATE developer_holdings SET tokens_held=tokens_held+?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                         (job["token_amount"], existing["id"]))
        else:
            conn.execute("""
                INSERT INTO developer_holdings(developer_id, company_id, asset_id, tokens_held)
                VALUES(?,?,?,?)
            """, (dev["id"], comp["id"], comp["asset_id"], job["token_amount"]))

        conn.execute("UPDATE jobs SET status='closed' WHERE id=?", (job_id,))
        conn.commit()
        conn.close()
        flash("Task verified and settled. Developer paid and tokens transferred.")
        return redirect(url_for("dashboard"))

    def refresh_all_prices():
        try:
            conn = db()
            companies = conn.execute("SELECT * FROM companies WHERE app_id IS NOT NULL AND name <> ''").fetchall()
            for comp in companies:
                user = conn.execute("SELECT * FROM users WHERE id=?", (comp["user_id"],)).fetchone()
                company_sk = algo_mnemonic.to_private_key(user["algo_mnemonic"])
                new_price_scaled = compute_token_price_scaled_gbp(
                    comp["name"], comp["supply"], comp["equity_pct"], valuation_override_gbp=comp["valuation_gbp"]
                )
                try:
                    update_token_price(company_sk, comp["app_id"], new_price_scaled)
                except Exception:
                    pass
            conn.close()
        except Exception:
            pass

    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(refresh_all_prices, "interval", minutes=5, id="price_updater", replace_existing=True)
    scheduler.start()

    return app

if __name__ == "__main__":
    app = create_app()
    app.run(debug=True)
