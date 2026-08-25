function emptyCounts() {
  return { queued: 0, running: 0, active: 0, failed_recent: 0 }
}

function emptySnapshot() {
  return {
    ok: false,
    online: false,
    service: "graphwing",
    units: {},
    units_healthy: false,
    api_active: false,
    counts: emptyCounts(),
    active: [],
    recent: [],
    workflows: [],
    error: "",
    code: ""
  }
}

function asJobs(value) {
  if (!value || typeof value.length !== "number") return []
  var out = []
  for (var i = 0; i < value.length; i++) {
    var row = value[i]
    if (!row || typeof row !== "object") continue
    out.push({
      job_id: String(row.job_id || ""),
      status: String(row.status || ""),
      kind: String(row.kind || "agent"),
      title: String(row.title || row.kind || "job"),
      repo: row.repo ? String(row.repo) : "",
      tab: String(row.tab || ""),
      herdr_tab_id: row.herdr_tab_id ? String(row.herdr_tab_id) : "",
      created_at: String(row.created_at || ""),
      started_at: String(row.started_at || ""),
      finished_at: String(row.finished_at || ""),
      error: row.error ? String(row.error) : "",
      summary: row.summary ? String(row.summary) : ""
    })
  }
  return out
}

function asCounts(value) {
  var c = emptyCounts()
  if (!value || typeof value !== "object") return c
  c.queued = Number(value.queued || 0)
  c.running = Number(value.running || 0)
  c.active = Number(value.active || 0)
  c.failed_recent = Number(value.failed_recent || 0)
  return c
}

function parseWatch(raw) {
  var text = String(raw || "").trim()
  if (text === "") {
    var blank = emptySnapshot()
    blank.error = "empty status"
    blank.code = "empty"
    return blank
  }
  var parsed
  try {
    parsed = JSON.parse(text)
  } catch (e) {
    var bad = emptySnapshot()
    bad.error = "Failed to parse graphwing status"
    bad.code = "bad_json"
    return bad
  }
  if (!parsed || typeof parsed !== "object") return emptySnapshot()
  var snap = emptySnapshot()
  snap.ok = parsed.ok === true
  snap.online = parsed.ok === true
  snap.service = String(parsed.service || "graphwing")
  snap.units = parsed.units && typeof parsed.units === "object" ? parsed.units : {}
  snap.units_healthy = parsed.units_healthy === true
  snap.api_active = parsed.api_active === true
  snap.counts = asCounts(parsed.counts)
  snap.active = asJobs(parsed.active)
  snap.recent = asJobs(parsed.recent)
  snap.workflows = asJobs(parsed.workflows && parsed.workflows.recent ? parsed.workflows.recent : [])
  snap.error = String(parsed.error || "")
  snap.code = String(parsed.code || "")
  if (parsed.ok === false && snap.error === "") snap.error = "graphwing error"
  return snap
}

function unitRows(units) {
  var names = ["graphwing-api", "graphwing-tunnel", "graphwing-herdr"]
  var rows = []
  var seen = {}
  for (var i = 0; i < names.length; i++) {
    var name = names[i]
    seen[name] = true
    var u = units && units[name] ? units[name] : {}
    var shortName = name.replace("graphwing-", "")
    var active = u.active === true
    var state = String(u.state || "unknown")
    var optional = shortName === "herdr"
    if (optional && !active) state = "optional"
    rows.push({
      name: shortName,
      active: active,
      optional: optional,
      state: state
    })
  }
  for (var extra in units) {
    if (seen[extra]) continue
    var x = units[extra] || {}
    rows.push({
      name: extra,
      active: x.active === true,
      state: String(x.state || "unknown")
    })
  }
  return rows
}

function parseMs(iso) {
  if (!iso) return 0
  var ms = Date.parse(String(iso))
  return isFinite(ms) ? ms : 0
}

function formatDuration(ms) {
  if (!(ms > 0)) return ""
  var sec = Math.floor(ms / 1000)
  var min = Math.floor(sec / 60)
  var hr = Math.floor(min / 60)
  if (hr > 0) return hr + "h " + (min % 60) + "m"
  if (min > 0) return min + "m"
  return Math.max(1, sec) + "s"
}

function jobElapsed(job, nowMs) {
  if (!job) return ""
  var start = parseMs(job.started_at || job.created_at)
  if (!start) return ""
  var end = parseMs(job.finished_at)
  if (!end) end = nowMs
  return formatDuration(end - start)
}

function jobDetail(job, nowMs) {
  if (!job) return ""
  var bits = [job.kind, job.status]
  if (job.repo) bits.push(job.repo)
  var elapsed = jobElapsed(job, nowMs)
  if (elapsed) bits.push(elapsed)
  if (job.error) bits.push(job.error)
  else if (job.summary) bits.push(job.summary)
  return bits.join(" · ")
}

function statusLabel(snap) {
  if (!snap || !snap.online) return snap && snap.error ? snap.error : "Daemon down"
  if (snap.counts && snap.counts.running > 0)
    return snap.counts.running + " running"
  if (snap.counts && snap.counts.queued > 0)
    return snap.counts.queued + " queued"
  if (snap.counts && snap.counts.failed_recent > 0)
    return snap.counts.failed_recent + " failed"
  if (snap.api_active) return "Idle"
  return "API unit down"
}
