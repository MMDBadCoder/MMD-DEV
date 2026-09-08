#!/usr/bin/env python3
"""Generate the MMD Grafana dashboards, one per administration tab.

Why generated
-------------
Hand-writing panel JSON is how a set of dashboards ends up with three panels
that disagree about units and two that query a metric nobody exports any more.
Everything here is built from one set of helpers, so a panel of a given kind is
always constructed the same way and the layout arithmetic is done once.

Why several dashboards rather than one
--------------------------------------
The single overview grew to 55 panels, which is a scrolling exercise rather
than a diagnosis. Each dashboard below answers the question its own admin tab
is already about, so the page you are on and the graphs on it agree.

Run: python3 observability/build_dashboards.py
"""
import json
import pathlib

DS = {"type": "prometheus", "uid": "mmd-prometheus"}
OUT = pathlib.Path(__file__).resolve().parent / "grafana" / "dashboards"

RED_IF_OLD = {"mode": "absolute", "steps": [
    {"color": "green", "value": None}, {"color": "orange", "value": 900},
    {"color": "red", "value": 3600}]}
RED_IF_ANY = {"mode": "absolute", "steps": [
    {"color": "green", "value": None}, {"color": "red", "value": 1}]}
AMBER_IF_ANY = {"mode": "absolute", "steps": [
    {"color": "green", "value": None}, {"color": "orange", "value": 1}]}

U = 'username=~"$username"'


class Board:
    """One dashboard, laid out top to bottom in full-width rows."""

    def __init__(self, uid, title, description=""):
        self.uid, self.title, self.description = uid, title, description
        self.panels, self.y, self.x = [], 0, 0

    # -- layout ----------------------------------------------------------
    def _place(self, w, h):
        if self.x + w > 24:
            self.x = 0
            self.y += self._rowh
        pos = {"h": h, "w": w, "x": self.x, "y": self.y}
        self.x += w
        self._rowh = h
        if self.x >= 24:
            self.x = 0
            self.y += h
        return pos

    def _targets(self, exprs):
        return [{"datasource": DS, "expr": e, "legendFormat": lf,
                 "refId": chr(65 + i), "range": True}
                for i, (e, lf) in enumerate(exprs)]

    # -- panels ----------------------------------------------------------
    def stat(self, title, exprs, w=6, h=4, unit="short", thresholds=None,
             text="auto", decimals=None, desc=""):
        self.panels.append({
            "type": "stat", "title": title, "datasource": DS,
            "description": desc,
            "gridPos": self._place(w, h), "targets": self._targets(exprs),
            "options": {"reduceOptions": {"calcs": ["lastNotNull"],
                                          "fields": "", "values": False},
                        "textMode": text, "colorMode": "value",
                        "graphMode": "area", "justifyMode": "auto"},
            "fieldConfig": {"defaults": {
                "unit": unit,
                **({"decimals": decimals} if decimals is not None else {}),
                "thresholds": thresholds or {"mode": "absolute", "steps": [
                    {"color": "text", "value": None}]}}, "overrides": []}})
        return self

    def ts(self, title, exprs, w=12, h=9, unit="short", stack=False, desc="",
           summary=True):
        """A time series. `summary` puts min/mean/max/last in a legend table
        under the lines, which is on by DEFAULT - for almost every chart here
        those four numbers are the first thing anyone asks. Pass
        `summary=False` where they would be noise, which is stacked counts:
        the mean of a stack member says very little."""
        self.panels.append({
            "type": "timeseries", "title": title, "datasource": DS,
            "description": desc, "gridPos": self._place(w, h),
            "targets": self._targets(exprs),
            "fieldConfig": {"defaults": {
                "unit": unit,
                "custom": {"drawStyle": "line", "lineWidth": 2,
                           "fillOpacity": 8, "showPoints": "never",
                           "stacking": {"mode": "normal" if stack else "none"}},
                "thresholds": {"mode": "absolute",
                               "steps": [{"color": "text", "value": None}]}},
                "overrides": []},
            "options": {"legend": {
                # A legend that carries min/mean/max IS the summary table, and
                # it sits against the lines it describes rather than in a
                # second panel that has to be read alongside.
                "displayMode": "table" if summary else "list",
                "placement": "bottom",
                "calcs": (["min", "mean", "max", "lastNotNull"] if summary
                          else ["lastNotNull"])},
                "tooltip": {"mode": "multi", "sort": "desc"}}})
        return self

    def table(self, title, exprs, w=12, h=8, desc="", value="Value",
              keep=("username",), unit="short", decimals=None):
        """An instant table, carrying only the columns a reader wants.

        A raw Prometheus table arrives with `Time`, `__name__`, `job` and
        `instance` beside the data - four columns that are identical on every
        row and mean nothing to whoever is looking. They are excluded here,
        and the value column is given the name of the thing it measures
        instead of the word "Value".
        """
        drop = ["Time", "__name__", "job", "instance", "le", "Value #A"]
        rename = {"Value": value}
        for label in keep:
            rename.setdefault(label, label)
        self.panels.append({
            "type": "table", "title": title, "datasource": DS,
            "description": desc, "gridPos": self._place(w, h),
            "targets": [{**t, "format": "table", "instant": True,
                         "range": False} for t in self._targets(exprs)],
            "transformations": [{
                "id": "organize",
                "options": {"excludeByName": {c: True for c in drop},
                            "renameByName": rename,
                            "indexByName": {}}}],
            "options": {"showHeader": True,
                        "sortBy": [{"displayName": value, "desc": True}]},
            "fieldConfig": {"defaults": {
                "custom": {"align": "auto"}, "unit": unit,
                **({"decimals": decimals} if decimals is not None else {})},
                "overrides": []}})
        return self

    def build(self, per_customer=True):
        tpl = [{
            "name": "username", "type": "query", "datasource": DS,
            "query": {"query": "label_values(mmd_user_credit_micro_toman, username)",
                      "refId": "vars"},
            "refresh": 2, "includeAll": True, "allValue": ".*", "multi": True,
            "current": {"text": ["All"], "value": ["$__all"]},
            "label": "Customer"}] if per_customer else []
        return {
            "annotations": {"list": []}, "editable": False, "graphTooltip": 1,
            "schemaVersion": 39, "tags": ["mmd"],
            "description": self.description,
            "templating": {"list": tpl},
            # An hour, not a day. These are operational dashboards: the
            # question is almost always "what is happening now", and a
            # 24-hour window flattens the spike you opened the page to find.
            "time": {"from": "now-1h", "to": "now"}, "refresh": "1m",
            "timezone": "browser", "title": self.title, "uid": self.uid,
            "version": 1, "panels": self.panels}


boards = []


# ------------------------------------------------------- fleet / health ---
b = Board("mmd-fleet", "MMD · Fleet health",
          "Is the platform running: services, the worker loop, backups.")
b.stat("Worker heartbeat", [("mmd_worker_heartbeat_age_seconds", "age")],
       unit="s", thresholds=RED_IF_OLD)
b.stat("Last good tick", [("mmd_worker_last_success_age_seconds", "age")],
       unit="s", thresholds=RED_IF_OLD)
b.stat("Services up", [("sum(mmd_process_up)", "up")], thresholds={
    "mode": "absolute", "steps": [{"color": "red", "value": None},
                                  {"color": "orange", "value": 3},
                                  {"color": "green", "value": 4}]})
b.stat("Schema patch failures", [("mmd_schema_patch_failures", "failed")],
       thresholds=RED_IF_ANY)
b.ts("Process CPU", [("rate(mmd_process_cpu_seconds_total[5m])", "{{unit}}")],
     unit="percentunit", desc="CPU seconds per second, per service.")
b.ts("Process memory", [("mmd_process_resident_bytes", "{{unit}}")], unit="bytes")
b.ts("Open file descriptors", [("mmd_process_open_fds", "{{unit}}")],
     desc="A steady climb is a leak.")
b.ts("Service restarts", [("mmd_process_restarts_total", "{{unit}}")],
     desc="Cumulative since boot. A climbing line is a crash loop.")
b.ts("Worker tick duration",
     [("histogram_quantile(0.95, sum by (le) (rate(mmd_worker_tick_seconds_bucket[15m])))", "p95"),
      ("mmd_worker_last_tick_seconds", "last")], unit="s")
b.ts("Worker tick failures",
     [("rate(mmd_worker_tick_failures_total[15m])", "failures/s")])
b.ts("Managed agents installed, by service",
     [("sum by (service) (mmd_workspace_service_ready)", "{{service}}")],
     desc="Hermes, OpenClaw, OpenCode and Open WebUI. Moved here from the "
          "supplier dashboard: whether an agent is installed is a question "
          "about the fleet, not about spend.")
boards.append((b, False))


# ---------------------------------------------------- customers / money ---
b = Board("mmd-customers", "MMD · Customers and credit",
          "Who exists, what they are worth, and who is about to stop.")
b.stat("Approved", [('sum(mmd_users{status="approved"})', "approved")])
b.stat("Pending", [('sum(mmd_users{status="pending"})', "pending")],
       thresholds=AMBER_IF_ANY)
b.stat("Total credit", [("sum(mmd_user_credit_micro_toman)/1000000", "Toman")])
b.stat("At zero", [("count(mmd_user_credit_micro_toman <= 0)", "accounts")],
       thresholds=AMBER_IF_ANY)
b.ts("Credit per customer",
     [(f"mmd_user_credit_micro_toman{{{U}}}/1000000", "{{username}}")],
     summary=True, desc="A line crossing zero is a machine about to stop.")
b.ts("Money moving through the ledger, per hour",
     [("sum by (kind) (increase(mmd_ledger_micro_toman[1h]))/1000000", "{{kind}}")],
     desc="Toman added or taken in each hour, split by what caused it. "
          "`grant` is credit you gave; every `charge_*` is money earned, and "
          "shows as a negative number because it leaves the customer's "
          "balance. Was 'Ledger movement by kind', which said neither what "
          "moved nor over what period.")
b.table("Customers closest to running out",
        [(f"bottomk(15, mmd_user_credit_micro_toman{{{U}}}/1000000)", "")],
        value="Toman", decimals=0,
        desc="Balance, lowest first. These are the accounts whose machines "
             "stop next.")
b.table("How many ledger rows of each kind exist",
        [("mmd_ledger_entries", "")], keep=("kind",), value="Rows",
        desc="A running count of transactions by type since the ledger began "
             "- not money, just how many times each kind has been written. "
             "Useful for spotting a charge type that has stopped happening.")
b.ts("Accounts by status", [("sum by (status) (mmd_users)", "{{status}}")],
     stack=True, summary=False)
b.ts("Each customer's machine, on or off over time",
     [(f"mmd_workspace_desired_on{{{U}}}", "{{username}}")], w=24, h=11,
     summary=False,
     desc="One line per customer: 1 while their machine is meant to be "
          "running, 0 while it is off. A snapshot table said only what is "
          "true this second; this shows WHEN each machine went up or down, "
          "which is the question that leads anywhere. Customers with no "
          "machine at all have no line - the chart below counts those.")
b.ts("Machines on over time",
     [("sum(mmd_workspace_desired_on)", "on"),
      ("sum(mmd_user_workspace_present)", "have a machine"),
      ("count(mmd_user_credit_micro_toman)", "accounts")], desc="How many machines are running, against how many exist and how many "
          "accounts there are.")
b.ts("Customers with a workspace",
     [("sum(mmd_user_workspace_present)", "with compute"),
      ("count(mmd_user_credit_micro_toman)", "accounts")],
     desc="An approved account without a workspace is a supported state.")
boards.append((b, True))


# ------------------------------------------------------ storage / capacity ---
b = Board("mmd-storage", "MMD · Storage and capacity",
          "Where the pool has gone and how far it is overcommitted.")
b.stat("Workspaces", [("sum(mmd_workspaces)", "total")])
b.stat("Running", [('sum(mmd_workspaces{state="on"})', "on")])
b.stat("Pool total", [("mmd_pool_total_gib", "GiB")], unit="decgbytes")
b.stat("Pool used", [("mmd_pool_used_gib", "GiB")], unit="decgbytes")
b.stat("Overcommit", [("mmd_pool_committed_gib / mmd_pool_total_gib", "x")],
       decimals=2,
       desc="Disk promised to customers divided by disk that exists. Above 1 "
            "is deliberate - that is thin provisioning - and stays safe while "
            "real usage sits well under the pool.")
b.stat("Pool free", [("mmd_pool_free_gib", "GiB")], unit="decgbytes",
       thresholds={"mode": "absolute", "steps": [
           {"color": "red", "value": None}, {"color": "orange", "value": 8},
           {"color": "green", "value": 20}]})
b.stat("Committed", [("mmd_pool_committed_gib", "GiB")], unit="decgbytes")
b.ts("Pool: used against free",
     [("mmd_pool_used_gib", "used"), ("mmd_pool_free_gib", "free"),
      ("mmd_pool_total_gib", "total")], unit="decgbytes", desc="The real ZFS pool over time. The tab used to draw a single bar of "
          "this moment, which cannot show a pool filling up.")
b.ts("Committed against the pool",
     [("mmd_pool_committed_gib", "committed to customers"),
      ("mmd_pool_total_gib", "pool size"),
      ("sum(mmd_workspace_disk_used_mib)/1024", "actually written")],
     unit="decgbytes",
     desc="Thin provisioning means committed legitimately exceeds the pool; "
          "the gap between the top two lines is the overcommit being relied on.")
b.ts("Where the pool has gone",
     [("sum(mmd_workspace_disk_used_mib)/1024", "customer machines"),
      ("mmd_pool_used_gib - sum(mmd_workspace_disk_used_mib)/1024",
       "platform: images, snapshots, overhead"),
      ("mmd_pool_free_gib", "free")], unit="decgbytes", stack=True,
     summary=False,
     desc="The three-way split the storage tab used to draw as one bar. "
          "Stacked, so the bands add up to the pool - and with history, so a "
          "band that is growing shows before it matters.")
b.table("Who is using the most disk?",
        [(f"topk(20, mmd_workspace_disk_used_mib{{{U}}}/1024)", "")],
        value="GiB written", unit="decgbytes",
        desc="What each machine has actually written, largest first - not "
             "what it is allowed to write.")
b.ts("Allocated CPU", [("sum(mmd_workspace_cpu_millicores)/1000", "vCPU")])
b.ts("Allocated memory", [("sum(mmd_workspace_memory_mib)/1024", "GiB")],
     unit="decgbytes")
b.ts("Workspaces by state", [("sum by (state) (mmd_workspaces)", "{{state}}")],
     stack=True, summary=False)
b.table("What state is each machine in?",
        [(f"mmd_workspace_state{{{U}}}", "")], keep=("username", "state"),
        value="", decimals=0,
        desc="One row per machine, naming its owner and its lifecycle state. "
             "The fleet counter says three are in error; this says which "
             "three.")
boards.append((b, True))


# ------------------------------------------------------------------ AI ---
b = Board("mmd-ai", "MMD · AI and supplier spend",
          "OpenRouter spend, key state and managed agent readiness.")
b.stat("Supplier usage", [("sum(mmd_user_openrouter_usage_usd)", "USD")],
       unit="currencyUSD")
b.stat("Keys ready", [("sum(mmd_user_openrouter_ready)", "ready")])
b.stat("Keys blocked", [("sum(mmd_user_openrouter_blocked)", "blocked")],
       thresholds=AMBER_IF_ANY)
# Agent readiness is not an OpenRouter question - whether Hermes is installed
# says nothing about supplier spend. It lives on the fleet dashboard, where
# "is this service up" belongs.
b.stat("Tokens this hour",
       [("sum(increase(mmd_ai_tokens_total[1h]))", "tokens")])
b.ts("Spend per customer, per hour",
     [(f"increase(mmd_user_openrouter_usage_usd{{{U}}}[1h])", "{{username}}")],
     unit="currencyUSD",
     desc="What each customer actually spent in each hour. The raw metric is "
          "a lifetime total, so plotted directly it only ever climbs and every "
          "customer looks equally busy; the hourly increase is the part that "
          "says who is spending NOW.")
b.ts("Which models are being used, per hour",
     [(f"sum by (model) (increase(mmd_ai_tokens_total{{{U}}}[1h]))", "{{model}}")],
     desc="Tokens per model per hour, across every managed agent. NOTE: this "
          "covers Claude and Codex, which report per-model usage from inside "
          "the workspace. OpenRouter bills one figure per key and does not "
          "break it down by model, so its spend appears in the panel above "
          "but cannot be split here.")
b.ts("Output tokens per model, per hour",
     [(f'sum by (model) (increase(mmd_ai_tokens_total{{{U},direction="output"}}[1h]))',
       "{{model}}")],
     desc="One line per model. Output only, because that is what dominates the "
          "bill - input and cache reads are priced far lower, and folding all "
          "three together gave every model the same shape.")
b.ts("Charged for AI, per hour",
     [(f"sum by (service) (increase(mmd_ai_billed_micro_toman_total{{{U}}}[1h]))/1000000",
       "{{service}}")],
     desc="Toman actually taken from customers for tokens, split by service.")
b.table("Whose AI key is switched off?",
        [(f"mmd_user_openrouter_blocked{{{U}}} > 0", "")],
        value="Blocked", decimals=0,
        desc="Keys disabled because the customer's balance reached zero. "
             "This is the panel that explains an agent that has gone quiet. "
             "An empty table is good news.")
b.ts("Supplier spend rate",
     [("sum(rate(mmd_user_openrouter_usage_usd[1h]))", "USD/s")],
     unit="currencyUSD")
boards.append((b, True))


# ---------------------------------------------------- API and operations ---
b = Board("mmd-api", "MMD · API and operations",
          "Request rates, latency, failures, and the privileged call path.")
b.stat("Operation backlog", [("mmd_operations_backlog", "queued+running")],
       thresholds={"mode": "absolute", "steps": [
           {"color": "green", "value": None}, {"color": "orange", "value": 5},
           {"color": "red", "value": 20}]})
b.stat("Failed operations", [('sum(mmd_operations{status="failed"})', "failed")],
       thresholds=RED_IF_ANY)
b.stat("Request rate", [("sum(rate(mmd_http_requests_total[5m]))", "req/s")],
       unit="reqps")
b.stat("5xx rate",
       [('sum(rate(mmd_http_requests_total{status="5xx"}[5m]))', "req/s")],
       unit="reqps", thresholds=RED_IF_ANY)
b.ts("Requests by status class",
     [("sum by (status) (rate(mmd_http_requests_total[5m]))", "{{status}}")],
     stack=True, summary=False, unit="reqps")
b.ts("Request latency",
     [("histogram_quantile(0.50, sum by (le) (rate(mmd_http_request_seconds_bucket[5m])))", "p50"),
      ("histogram_quantile(0.95, sum by (le) (rate(mmd_http_request_seconds_bucket[5m])))", "p95"),
      ("histogram_quantile(0.99, sum by (le) (rate(mmd_http_request_seconds_bucket[5m])))", "p99")],
     unit="s")
b.table("Which endpoints are slowest?",
        [("topk(15, histogram_quantile(0.95, sum by (le, route) "
          "(rate(mmd_http_request_seconds_bucket[30m]))))", "")],
        keep=("route",), value="p95 seconds", unit="s",
        desc="95th-percentile response time per endpoint over the last half "
             "hour: nineteen requests in twenty were faster than this.")
b.ts("Errors by route",
     [('topk(10, sum by (route, status) (rate(mmd_http_requests_total{status=~"4xx|5xx"}[5m])))',
       "{{route}} {{status}}")], unit="reqps")
b.ts("Authentication failures",
     [("sum by (method, reason) (rate(mmd_auth_failures_total[15m]))",
       "{{method}} {{reason}}")], unit="reqps",
     desc="Reason, never which account.")
b.ts("Registration conflicts",
     [("sum by (field) (rate(mmd_registration_conflicts_total[1h]))", "{{field}}")])
b.ts("Operations by kind and status",
     [("sum by (kind, status) (mmd_operations)", "{{kind}} {{status}}")])
b.ts("Provisioner calls by result",
     [("sum by (result) (rate(mmd_provisioner_calls_total[15m]))", "{{result}}")],
     stack=True, summary=False)
b.table("How long does each privileged action take?",
        [("histogram_quantile(0.95, sum by (le, verb) "
          "(rate(mmd_provisioner_seconds_bucket[1h])))", "")],
        keep=("verb",), value="p95 seconds", unit="s",
        desc="Every action the control plane asks the root provisioner to "
             "perform, and how long the slow ones take. Creating a machine "
             "legitimately takes minutes; a status check should not.")
b.ts("Handler exceptions",
     [("sum by (method) (rate(mmd_http_exceptions_total[15m]))", "{{method}}")])
boards.append((b, False))


# --------------------------------------------------- capacity / tariffs ---
# Replaces three charts the تعرفه‌ها و ظرفیت tab used to draw itself: host CPU,
# host memory, and the per-workspace series beneath them. Those were redrawn
# from `/api/admin/metrics` on every visit and only ever showed the window the
# picker happened to be on; here the same data has real history and a shared
# time range.
b = Board("mmd-capacity", "MMD · Capacity and headroom",
          "What the scheduler will admit, and what the fleet is really using.")
b.stat("Running", [("mmd_capacity_running", "workspaces")])
b.stat("Cores used", [("mmd_capacity_used_cores", "used")])
b.stat("Cores schedulable", [("mmd_capacity_schedulable_cores", "schedulable")])
b.stat("Memory used", [("mmd_capacity_used_memory_gib", "GiB")], unit="decgbytes")
b.ts("CPU: allocated against schedulable",
     [("mmd_capacity_used_cores", "allocated"),
      ("mmd_capacity_schedulable_cores", "schedulable"),
      ("mmd_capacity_total_cores", "physical")],
     desc="Schedulable exceeds physical by the overcommit ratio; that is the "
          "policy, not a fault.")
b.ts("Memory: allocated against schedulable",
     [("mmd_capacity_used_memory_gib", "allocated"),
      ("mmd_capacity_schedulable_memory_gib", "schedulable"),
      ("mmd_capacity_total_memory_gib", "physical")], unit="decgbytes")
b.ts("CPU actually burned, per workspace",
     [(f"rate(mmd_workspace_cpu_seconds_total{{{U}}}[15m])", "{{username}}")],
     desc="Cores in use. Differentiated by Prometheus from the raw counter, "
          "so the window is yours to choose rather than baked in.")
b.ts("Memory in use, per workspace",
     [(f"mmd_workspace_memory_bytes{{{U}}}", "{{username}}")], unit="bytes")
b.ts("Allocated vs used CPU",
     [("sum(mmd_workspace_cpu_millicores)/1000", "allocated cores"),
      ("sum(rate(mmd_workspace_cpu_seconds_total[15m]))", "cores burned")],
     desc="The gap is what overcommit is selling.")
b.table("How old is each machine's last reading?",
        [(f"topk(10, mmd_workspace_sample_age_seconds{{{U}}})", "")],
        value="Seconds ago", unit="s", decimals=0,
        desc="Usage is only measured while a machine runs, so a switched-off "
             "machine's reading ages forever - a large number here usually "
             "just means 'off'. It matters for machines you believe are ON: "
             "there, a growing age means metering has stopped and that "
             "customer is not being billed. Was called 'Stale samples'.")
boards.append((b, True))


# --------------------------------------------------------------- support ---
b = Board("mmd-support", "MMD · Support",
          "Ticket queue, response age and unread notifications.")
b.stat("Open", [('sum(mmd_tickets{status="open"})', "open")],
       thresholds=AMBER_IF_ANY)
b.stat("Waiting for user", [('sum(mmd_tickets{status="waiting_for_user"})', "wfu")])
b.stat("Oldest open", [("mmd_ticket_oldest_open_seconds", "age")], unit="s",
       thresholds=RED_IF_OLD)
b.stat("Unread by staff", [("mmd_tickets_unread_staff", "unread")],
       thresholds=AMBER_IF_ANY)
b.ts("Tickets by status", [("sum by (status) (mmd_tickets)", "{{status}}")],
     stack=True, summary=False)
b.ts("Oldest open ticket age", [("mmd_ticket_oldest_open_seconds", "age")],
     unit="s", desc="The SLO number.")
b.ts("Unread notifications by kind",
     [("sum by (kind) (mmd_notifications_unread)", "{{kind}}")], stack=True, summary=False)
b.ts("Oldest unread notification",
     [("mmd_notification_oldest_unread_seconds", "age")], unit="s")
boards.append((b, False))


# -------------------------------------------------------------- delivery ---
b = Board("mmd-delivery", "MMD · Backups and messaging",
          "Whether backups are arriving and SMS is being delivered.")
b.stat("Backup age", [("mmd_backup_age_seconds", "age")], unit="s",
       thresholds={"mode": "absolute", "steps": [
           {"color": "green", "value": None},
           {"color": "orange", "value": 172800},
           {"color": "red", "value": 259200}]})
b.stat("Backup size", [("mmd_backup_last_size_bytes", "bytes")], unit="bytes")
b.stat("Backup enabled", [("mmd_backup_enabled", "armed")])
b.stat("Backup failing", [("mmd_backup_failing", "failing")],
       thresholds=RED_IF_ANY)
b.ts("Backup age over time", [("mmd_backup_age_seconds", "age")], unit="s",
     desc="A sawtooth is healthy; a straight climb means delivery stopped.")
b.ts("Backup size", [("mmd_backup_last_size_bytes", "bytes")], unit="bytes")
b.ts("SMS outbox by status",
     [("sum by (status) (mmd_sms_messages)", "{{status}}")], stack=True, summary=False,
     desc="`skipped` is the temporary trial allowlist, not a failure.")
b.ts("SMS by kind", [("sum by (kind) (mmd_sms_messages)", "{{kind}}")])
boards.append((b, False))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.json"):
        old.unlink()
    for board, per_customer in boards:
        doc = board.build(per_customer)
        (OUT / f"{board.uid}.json").write_text(json.dumps(doc, indent=2) + "\n")
        print(f"{board.uid:16s} {len(board.panels):3d} panels  {board.title}")
    print(f"\n{len(boards)} dashboards -> {OUT}")


if __name__ == "__main__":
    main()
