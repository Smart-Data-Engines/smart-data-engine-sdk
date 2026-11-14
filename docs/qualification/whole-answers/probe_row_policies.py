"""What a ClickHouse row policy does to a login's reads, and what that login can find out.

A policy for the login, for a role it holds or for all but another login answers it some of a
table's rows and no error. The catalogue of policies needs a grant of its own; the plan of the
login's own read names the filter where one applies to it. Plain HTTP and the standard library.

    python3 probe_row_policies.py http://127.0.0.1:58124 default probe > row-policies-24.8.json

against a server started as ``probe_login_settings.py`` says.
"""
import json, sys, urllib.parse, urllib.request, urllib.error, base64

base, admin, password = sys.argv[1], sys.argv[2], sys.argv[3]

def ask(sql, user=admin, pw=password, **settings):
    q = urllib.parse.urlencode({"database": "probe", **settings})
    req = urllib.request.Request(f"{base}/?{q}", data=sql.encode(), method="POST")
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read().decode().strip()
    except urllib.error.HTTPError as e:
        return "ERROR " + e.read().decode().strip().splitlines()[0][:230]

out = {"version": ask("SELECT version()")}
for s in ("DROP ROW POLICY IF EXISTS hide ON probe.p", "DROP ROW POLICY IF EXISTS others ON probe.p",
          "DROP ROW POLICY IF EXISTS open ON probe.p", "DROP TABLE IF EXISTS probe.p",
          "DROP USER IF EXISTS p_op", "DROP USER IF EXISTS p_other", "DROP ROLE IF EXISTS p_role"):
    ask(s)
ask("CREATE TABLE probe.p (id Int64, value Int32) ENGINE = ReplacingMergeTree ORDER BY id")
ask("INSERT INTO probe.p SELECT number, number FROM numbers(100)")
ask("CREATE USER p_op IDENTIFIED WITH plaintext_password BY 'pw'")
ask("CREATE USER p_other IDENTIFIED WITH plaintext_password BY 'pw'")
ask("GRANT SELECT, INSERT, ALTER ON probe.p TO p_op")
ask("GRANT SELECT ON probe.p TO p_other")

def look(label):
    one = {}
    one["count"] = ask("SELECT count() FROM probe.p FINAL", "p_op", "pw")
    one["catalog"] = ask("SELECT name, select_filter, is_restrictive, apply_to_all, apply_to_list, apply_to_except "
                         "FROM system.row_policies WHERE database = 'probe' AND table = 'p' FORMAT JSONCompactEachRow", "p_op", "pw")
    one["show"] = ask("SHOW ROW POLICIES ON probe.p", "p_op", "pw")
    plan = ask("EXPLAIN actions = 1 SELECT * FROM probe.p FINAL", "p_op", "pw")
    one["explain_mentions"] = sorted({l.strip()[:80] for l in plan.splitlines() if "ow level" in l or "ow-level" in l or "olicy" in l})
    one["explain_error"] = plan[:200] if plan.startswith("ERROR") else ""
    one["insert_hidden_row"] = ask("INSERT INTO probe.p VALUES (5000, 1)", "p_op", "pw") or "ok"
    ask("ALTER TABLE probe.p DELETE WHERE id = 5000 SETTINGS mutations_sync = 2")
    out[label] = one

look("no policy")
ask("CREATE ROW POLICY hide ON probe.p FOR SELECT USING id < 10 TO p_op")
look("a policy that hides 90 rows from the operator's login")
ask("DROP ROW POLICY hide ON probe.p")
ask("CREATE ROW POLICY others ON probe.p FOR SELECT USING id < 10 TO p_other")
look("a policy for another login only")
ask("CREATE ROW POLICY open ON probe.p FOR SELECT USING 1 TO p_op")
look("a policy for another login and USING 1 for the operator's")
ask("DROP ROW POLICY others ON probe.p"); ask("DROP ROW POLICY open ON probe.p")
ask("CREATE ROLE p_role"); ask("GRANT p_role TO p_op")
ask("CREATE ROW POLICY hide ON probe.p FOR SELECT USING id < 10 TO p_role")
look("a policy on a role the operator's login holds")
ask("DROP ROW POLICY hide ON probe.p")
ask("CREATE ROW POLICY hide ON probe.p FOR SELECT USING id < 10 TO ALL EXCEPT p_other")
look("a policy for all except another login")
ask("GRANT SHOW ROW POLICIES ON probe.p TO p_op")
look("the same, and the operator's login may show row policies")
for s in ("DROP ROW POLICY IF EXISTS hide ON probe.p", "DROP TABLE IF EXISTS probe.p",
          "DROP USER IF EXISTS p_op", "DROP USER IF EXISTS p_other", "DROP ROLE IF EXISTS p_role"):
    ask(s)
print(json.dumps(out, indent=1))
