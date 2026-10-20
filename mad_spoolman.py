# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "MAD-Spoolman"
# description = "Links filament presets to Spoolman spools and writes the active spool macro into the G-code before printing."
# author = "MarkovDA"
# version = "1.0.0"
# ///
"""Bind OrcaSlicer filament presets to Spoolman spools.

The Spoolman tab lists the filament presets and lets you pick a spool for each
one. Bindings live in the plugin's own data folder; presets are never modified.

While a plate is sliced, the "Spoolman G-code" capability inserts the configured
macro with the bound spool ID into the exported G-code, right after the filament
start marker. It has to be selected once per process preset in
Process > Others > Slicing Pipeline Plugin.
"""

import json
import os
import re
import sys
import time
import urllib.request

import orca

BUNDLE_ID = "mad-spoolman"
BUNDLE_NAME = "Spoolman"
MARK_BEGIN = "; >>> spoolman"
MARK_END = "; <<< spoolman"
MAP_FILE = "bindings.json"
PRESET_VERSION = "2.5.0.0"
DEFAULT_SETTINGS = {
    "url": "http://localhost:7912",
    "macro": "SET_ACTIVE_SPOOL ID={id}",
}

STRINGS = {
    "en": {
        "address": "Spoolman address",
        "macro": "Macro command",
        "save": "Save",
        "check": "Check connection",
        "presets": "Filament presets → spools",
        "refresh": "Refresh",
        "sync": "Match spools to presets",
        "col_preset": "Preset",
        "col_spool": "Spool",
        "col_material": "Material",
        "col_remaining": "Remaining, g",
        "not_bound": "— not bound —",
        "apply": "Apply",
        "unbind": "Unbind",
        "from_spoolman": "from Spoolman",
        "loading": "loading…",
        "no_presets": "no presets",
        "taken": " (in use)",
        "missing": " (not in Spoolman)",
        "hint": "The command is inserted while slicing. Select “Spoolman G-code” in the process preset under "
                "Process → Others → Slicing Pipeline Plugin — once per preset.",
        "saved": "settings saved",
        "connected": "Spoolman {version} ({db}), connection OK",
        "unknown": "unknown action: {action}",
        "bound": "“{preset}” → spool #{id} {spool}",
        "unbound": "“{preset}” unbound",
        "matched": "matched existing presets: {list}",
        "created": "created presets: {list}",
        "all_matched": "all spools are already matched",
        "reload_failed": " (could not reload the bundle: {error})",
        "spool_missing": "spool {id} is not found in Spoolman",
        "no_preset": "no preset given",
        "macro_needs_id": "the macro command has no {id} placeholder",
    },
    "ru": {
        "address": "Адрес Spoolman",
        "macro": "Команда для макроса",
        "save": "Сохранить",
        "check": "Проверить связь",
        "presets": "Профили филамента → катушки",
        "refresh": "Обновить",
        "sync": "Разложить катушки по профилям",
        "col_preset": "Профиль",
        "col_spool": "Катушка",
        "col_material": "Материал",
        "col_remaining": "Остаток, г",
        "not_bound": "— не привязана —",
        "apply": "Применить",
        "unbind": "Отвязать",
        "from_spoolman": "из Spoolman",
        "loading": "загрузка…",
        "no_presets": "профилей нет",
        "taken": " (занята)",
        "missing": " (нет в Spoolman)",
        "hint": "Команда вставляется при слайсинге. Выберите «Spoolman G-code» в process-профиле: "
                "Process → Others → Slicing Pipeline Plugin — один раз на профиль.",
        "saved": "настройки сохранены",
        "connected": "Spoolman {version} ({db}), связь есть",
        "unknown": "неизвестное действие: {action}",
        "bound": "«{preset}» → катушка #{id} {spool}",
        "unbound": "профиль «{preset}» отвязан",
        "matched": "привязаны к готовым профилям: {list}",
        "created": "созданы профили: {list}",
        "all_matched": "все катушки уже разложены",
        "reload_failed": " (перечитать бандл не удалось: {error})",
        "spool_missing": "катушка {id} не найдена в Spoolman",
        "no_preset": "не указан профиль",
        "macro_needs_id": "в шаблоне команды нет подстановки {id}",
    },
}


def log(message):
    sys.stderr.write(f"[mad-spoolman] {message}\n")
    sys.stderr.flush()


def ui_language():
    try:
        info = orca.host.app_info()
    except Exception:  # noqa: BLE001
        return "en"
    code = str((info or {}).get("language") or "")
    return "ru" if code.lower().startswith("ru") else "en"


def data_dir():
    storage = orca.host.plugin.storage()
    return os.path.dirname(os.path.dirname(os.path.dirname(storage)))


def storage_dir():
    path = orca.host.plugin.storage()
    os.makedirs(path, exist_ok=True)
    return path


def user_dir():
    bundle = orca.host.preset_bundle()
    for name in bundle.filaments.preset_names():
        preset = bundle.filaments.find_preset(name)
        if preset is not None and preset.is_user() and preset.file:
            return os.path.dirname(os.path.dirname(preset.file))
    root = os.path.join(data_dir(), "user")
    candidates = [
        entry for entry in os.listdir(root)
        if os.path.isdir(os.path.join(root, entry)) and entry not in ("_local", "default")
    ]
    if not candidates:
        raise RuntimeError("no user preset folder found")
    return os.path.join(root, candidates[0])


def load_settings():
    path = os.path.join(storage_dir(), "settings.json")
    settings = dict(DEFAULT_SETTINGS)
    try:
        with open(path, encoding="utf-8") as fh:
            settings.update(json.load(fh))
    except (OSError, ValueError):
        pass
    return settings


def save_settings(values, tr):
    settings = dict(DEFAULT_SETTINGS)
    settings["url"] = (values.get("url") or DEFAULT_SETTINGS["url"]).strip().rstrip("/")
    settings["macro"] = (values.get("macro") or DEFAULT_SETTINGS["macro"]).strip()
    if "{id}" not in settings["macro"]:
        raise ValueError(tr["macro_needs_id"])
    with open(os.path.join(storage_dir(), "settings.json"), "w", encoding="utf-8") as fh:
        json.dump(settings, fh, ensure_ascii=False, indent=2)
    return settings


def api_get(settings, path):
    request = urllib.request.Request(settings["url"] + path, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_spools(settings):
    spools = api_get(settings, "/api/v1/spool?allow_archived=false")
    views = []
    for spool in spools:
        filament = spool.get("filament") or {}
        views.append(
            {
                "id": spool.get("id"),
                "material": filament.get("material") or "",
                "name": filament.get("name") or "",
                "vendor": (filament.get("vendor") or {}).get("name") or "",
                "color": filament.get("color_hex") or "",
                "remaining": round(spool.get("remaining_weight") or 0),
            }
        )
    views.sort(key=lambda item: (item["material"], item["vendor"], item["name"], item["id"]))
    return views


def spool_alias(spool):
    alias = " ".join(part for part in (spool["vendor"], spool["name"]) if part).strip()
    return alias or f"Spool {spool['id']}"


def load_map():
    try:
        with open(os.path.join(storage_dir(), MAP_FILE), encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        raw = {}
    result = {}
    for profile, record in raw.items():
        if isinstance(record, dict) and record.get("spool_id") is not None:
            result[profile] = {
                "spool_id": int(record["spool_id"]),
                "spool_label": record.get("spool_label") or "",
            }
    return result


def save_map(bindings):
    with open(os.path.join(storage_dir(), MAP_FILE), "w", encoding="utf-8") as fh:
        json.dump(bindings, fh, ensure_ascii=False, indent=2)


def bind_profile(profile, spool):
    bindings = load_map()
    bindings[profile] = {"spool_id": spool["id"], "spool_label": spool_alias(spool)}
    save_map(bindings)
    log(f"bound {profile!r} to spool {spool['id']}")


def unbind_profile(profile):
    bindings = load_map()
    if bindings.pop(profile, None) is None:
        return False
    save_map(bindings)
    log(f"unbound {profile!r}")
    return True


def short_name(name):
    prefix = f"_local/{BUNDLE_ID}/"
    return name[len(prefix):] if name.startswith(prefix) else name


def all_filaments():
    bundle = orca.host.preset_bundle()
    result = []
    for name in bundle.filaments.preset_names():
        preset = bundle.filaments.find_preset(name)
        if preset is None:
            continue
        is_bundle = preset.bundle_id == BUNDLE_ID
        if not preset.is_user() and not is_bundle:
            continue
        result.append(
            {
                "name": name,
                "label": short_name(name) if is_bundle else (preset.label() or name),
                "is_user": preset.is_user(),
                "is_bundle": is_bundle,
            }
        )
    result.sort(key=lambda item: (item["is_bundle"], item["label"].lower()))
    return result


def material_parent(material, spool, profiles):
    vendor = (spool["vendor"] or "").lower()
    filament = (spool["name"] or "").lower()
    best, best_score = None, 0
    for profile in profiles:
        if profile["is_bundle"]:
            continue
        low = profile["label"].lower()
        score = 0
        if vendor and vendor in low:
            score += 2
        if filament and filament in low:
            score += 2
        if material and material.lower() in low:
            score += 1
        if score > best_score:
            best, best_score = profile["label"], score
    if best is not None and best_score >= 3:
        return best
    names = orca.host.preset_bundle().filaments.preset_names()
    generic = [name for name in names if name.startswith(f"Generic {material}")]
    for name in generic:
        if "@Flashforge" in name:
            return name
    for name in generic:
        if "@" in name:
            return name
    if generic:
        return generic[0]
    return "Default Filament"


def best_match(spool, profiles):
    vendor = (spool["vendor"] or "").lower()
    filament = (spool["name"] or "").lower()
    best, best_score = None, 0
    for profile in profiles:
        low = profile["label"].lower()
        score = 0
        if vendor and vendor in low:
            score += 2
        if filament and filament in low:
            score += 2
        if spool["material"] and spool["material"].lower() in low:
            score += 1
        if score > best_score:
            best, best_score = profile, score
    return best if best_score >= 3 else None


def bundle_dir():
    return os.path.join(user_dir(), "_local", BUNDLE_ID)


def slug(text):
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(".")
    return cleaned[:80] or "spool"


def slug_taken(stem):
    folder = os.path.join(bundle_dir(), "filament")
    if not os.path.isdir(folder):
        return False
    lowered = stem.lower()
    return any(os.path.splitext(name)[0].lower() == lowered for name in os.listdir(folder))


def write_bundle_preset(spool, parent):
    stem = slug(spool_alias(spool))
    candidate, counter = stem, 2
    while slug_taken(candidate):
        candidate = f"{stem} {counter}"
        counter += 1
    data = {
        "name": candidate,
        "from": "Bundle",
        "inherits": parent,
        "filament_settings_id": [candidate],
        "version": PRESET_VERSION,
    }
    if spool["material"]:
        data["filament_type"] = [spool["material"]]
    folder = os.path.join(bundle_dir(), "filament")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, candidate + ".json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    write_bundle_metadata()
    log(f"created preset {candidate!r} inheriting {parent!r}")
    return f"_local/{BUNDLE_ID}/{candidate}"


def write_bundle_metadata():
    folder = bundle_dir()
    fil_folder = os.path.join(folder, "filament")
    os.makedirs(fil_folder, exist_ok=True)
    stems = sorted(os.path.splitext(name)[0] for name in os.listdir(fil_folder) if name.endswith(".json"))
    metadata = {
        "author": "MAD-Spoolman",
        "description": "Filament presets created by MAD-Spoolman",
        "filament_presets": stems,
        "id": BUNDLE_ID,
        "imported_time": int(time.time()),
        "name": BUNDLE_NAME,
        "print_presets": [],
        "printer_presets": [],
        "updated_time": int(time.time()),
        "version": "1.0",
    }
    with open(os.path.join(folder, "bundle_metadata.json"), "w", encoding="utf-8") as fh:
        json.dump(metadata, fh, ensure_ascii=False, indent=2)


def reload_bundle():
    try:
        orca.host.preset.bundles.reload_local(BUNDLE_ID)
        return None
    except Exception as exc:  # noqa: BLE001
        log(f"reload_local failed: {type(exc).__name__}: {exc}")
        return str(exc)


def macro_line(settings, spool_id):
    return settings["macro"].replace("{id}", str(spool_id))


def inject_macro(path, line):
    # newline="" keeps the file's original line endings intact.
    with open(path, encoding="utf-8", errors="surrogateescape", newline="") as fh:
        text = fh.read()
    if MARK_BEGIN in text or line in text:
        log(f"{os.path.basename(path)} already carries the command")
        return False
    lines = text.splitlines(keepends=True)
    position = None
    for index, value in enumerate(lines):
        if value.strip() == "; filament start gcode":
            position = index + 1
            break
    if position is None:
        for index, value in enumerate(lines):
            if value.startswith(";LAYER_CHANGE"):
                position = index
                break
    if position is None:
        log(f"no insertion point found in {os.path.basename(path)}")
        return False
    lines.insert(position, f"{MARK_BEGIN}\n{line}\n{MARK_END}\n")
    with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as fh:
        fh.write("".join(lines))
    log(f"inserted into {os.path.basename(path)} at line {position + 1}: {line}")
    return True


def filament_names_from_ctx(ctx):
    raw = ctx.config_value("filament_settings_id")
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return [raw]
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return [str(raw)]


def state_payload(tr):
    settings = load_settings()
    payload = {"ok": True, "settings": settings, "spools": [], "profiles": [], "bindings": {}}
    try:
        profiles = all_filaments()
        bindings = load_map()
        known = {profile["name"] for profile in profiles}
        stale = [name for name in bindings if name not in known]
        if stale:
            for name in stale:
                log(f"dropped binding to missing preset {name!r}")
                bindings.pop(name, None)
            save_map(bindings)
        spool_ids = set()
        for profile in profiles:
            record = bindings.get(profile["name"])
            profile["spool_id"] = record["spool_id"] if record else None
            profile["spool_label"] = record["spool_label"] if record else ""
            if record:
                spool_ids.add(record["spool_id"])
        payload["profiles"] = profiles
        payload["bindings"] = {name: record["spool_id"] for name, record in bindings.items()}
        spools = fetch_spools(settings)
        for spool in spools:
            spool["bound"] = spool["id"] in spool_ids
            spool["alias"] = spool_alias(spool)
        payload["spools"] = spools
    except Exception as exc:  # noqa: BLE001
        payload["ok"] = False
        payload["error"] = f"{type(exc).__name__}: {exc}"
        log(f"state failed: {payload['error']}")
    return payload


def page(tr):
    html = PAGE
    for key, value in tr.items():
        html = html.replace(f"@@{key}@@", value)
    return html


PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<style>
  body { font-family: var(--orca-font); color: var(--orca-fg); background: var(--orca-bg);
         margin: 0; padding: 14px 16px 28px; font-size: 13px; }
  h2 { font-size: 15px; margin: 0 0 10px; }
  h3 { font-size: 13px; margin: 18px 0 8px; color: var(--orca-muted); text-transform: uppercase; letter-spacing: .04em; }
  .row { display: flex; gap: 10px; align-items: flex-end; flex-wrap: wrap; margin-bottom: 10px; }
  label { display: flex; flex-direction: column; gap: 3px; color: var(--orca-muted); font-size: 12px; }
  input { background: var(--orca-bg); color: var(--orca-fg); border: 1px solid var(--orca-border);
          border-radius: 4px; padding: 5px 7px; font-family: inherit; font-size: 13px; }
  input.url, input.macro { width: 300px; }
  button { background: transparent; color: var(--orca-fg); border: 1px solid var(--orca-border);
           border-radius: 4px; padding: 5px 10px; cursor: pointer; font-family: inherit; font-size: 13px; }
  button:hover { border-color: var(--orca-accent); color: var(--orca-accent); }
  button.primary { background: var(--orca-accent); color: var(--orca-accent-fg); border-color: var(--orca-accent); }
  select { background: var(--orca-bg); color: var(--orca-fg); border: 1px solid var(--orca-border);
           border-radius: 4px; padding: 4px 6px; font-family: inherit; font-size: 12px; max-width: 330px; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--orca-border); vertical-align: middle; }
  th { color: var(--orca-muted); font-weight: normal; font-size: 12px; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  .muted { color: var(--orca-muted); }
  .status { margin: 10px 0 0; min-height: 18px; white-space: pre-wrap; }
  .status.err { color: #e06c6c; }
  .chip { display: inline-block; padding: 1px 6px; border: 1px solid var(--orca-border); border-radius: 9px; font-size: 11px; }
</style></head>
<body>
<h2>Spoolman</h2>
<div class="row">
  <label>@@address@@<input id="url" class="url" placeholder="http://localhost:7912"></label>
  <label>@@macro@@<input id="macro" class="macro" placeholder="SET_ACTIVE_SPOOL ID={id}"></label>
  <button id="save">@@save@@</button>
  <button id="check">@@check@@</button>
</div>

<h3>@@presets@@</h3>
<div class="row">
  <button id="refresh">@@refresh@@</button>
  <button id="sync" class="primary">@@sync@@</button>
</div>
<table>
  <thead><tr><th>@@col_preset@@</th><th>@@col_spool@@</th><th>@@col_material@@</th>
    <th class="num">@@col_remaining@@</th><th></th></tr></thead>
  <tbody id="rows"><tr><td colspan="5" class="muted">@@loading@@</td></tr></tbody>
</table>
<p class="muted">@@hint@@</p>
<div id="status" class="status muted"></div>

<script>
const $ = id => document.getElementById(id);
const T = {
  notBound: "@@not_bound@@",
  apply: "@@apply@@",
  unbind: "@@unbind@@",
  fromSpoolman: "@@from_spoolman@@",
  noPresets: "@@no_presets@@",
  taken: "@@taken@@",
  missing: "@@missing@@"
};
let state = null;

function esc(text) {
  return String(text == null ? '' : text).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
function status(text, isError) {
  $('status').textContent = text || '';
  $('status').className = 'status ' + (isError ? 'err' : 'muted');
}
function spoolById(id) {
  return (state.spools || []).find(s => s.id === id) || null;
}
function render(next) {
  state = next;
  if (next.settings) { $('url').value = next.settings.url || ''; $('macro').value = next.settings.macro || ''; }
  if (!next.ok) { status(next.error || '', true); }
  const spools = next.spools || [];
  const rows = (next.profiles || []).map(profile => {
    const current = profile.spool_id;
    const options = [`<option value="">${esc(T.notBound)}</option>`];
    for (const spool of spools) {
      const selected = spool.id === current ? ' selected' : '';
      const used = spool.bound && spool.id !== current ? esc(T.taken) : '';
      options.push(`<option value="${spool.id}"${selected}>#${spool.id} ${esc(spool.alias)} — ${esc(spool.material)}${used}</option>`);
    }
    if (current && !spoolById(current)) {
      options.push(`<option value="${current}" selected>#${current} ${esc(profile.spool_label)}${esc(T.missing)}</option>`);
    }
    const spool = spoolById(current);
    const material = spool ? esc(spool.material) : '<span class="muted">—</span>';
    const remaining = spool ? spool.remaining : '<span class="muted">—</span>';
    const badge = profile.is_bundle ? ` <span class="chip">${esc(T.fromSpoolman)}</span>` : '';
    return `<tr>
      <td>${esc(profile.label)}${badge}</td>
      <td><select data-profile="${esc(profile.name)}">${options.join('')}</select></td>
      <td>${material}</td>
      <td class="num">${remaining}</td>
      <td style="white-space:nowrap">
        <button data-bind="${esc(profile.name)}">${esc(T.apply)}</button>
        ${current ? `<button data-unbind="${esc(profile.name)}">${esc(T.unbind)}</button>` : ''}
      </td></tr>`;
  });
  $('rows').innerHTML = rows.length ? rows.join('') : `<tr><td colspan="5" class="muted">${esc(T.noPresets)}</td></tr>`;
}
function send(message) { orca.postMessage(message); }

orca.onMessage(data => {
  if (data.type === 'state') render(data.payload);
  if (data.type === 'status') status(data.text, data.error);
});
$('save').onclick = () => send({type: 'save_settings', url: $('url').value, macro: $('macro').value});
$('check').onclick = () => send({type: 'check'});
$('refresh').onclick = () => send({type: 'state'});
$('sync').onclick = () => send({type: 'sync'});
$('rows').onclick = event => {
  const bind = event.target.getAttribute('data-bind');
  const unbind = event.target.getAttribute('data-unbind');
  if (unbind) { send({type: 'unbind', profile: unbind}); return; }
  if (bind) {
    const select = Array.from(document.querySelectorAll('select[data-profile]'))
      .find(node => node.getAttribute('data-profile') === bind);
    send({type: 'bind', profile: bind, spool_id: select && select.value ? Number(select.value) : null});
  }
};
send({type: 'state'});
</script>
</body></html>
"""


class SpoolmanPage(orca.pages.PagesPluginCapabilityBase):
    def get_name(self):
        return "Spoolman"

    def get_ui(self):
        self._lang = ui_language()
        log(f"ui language: {self._lang}")
        return page(STRINGS[self._lang])

    def tr(self, key, **kwargs):
        text = STRINGS.get(getattr(self, "_lang", "en"), STRINGS["en"])[key]
        return text.format(**kwargs) if kwargs else text

    def on_message(self, data):
        action = (data or {}).get("type")
        log(f"message {action}")
        tr = lambda key, **kw: self.tr(key, **kw)
        try:
            if action == "state":
                self.post_state()
            elif action == "save_settings":
                save_settings(data, STRINGS[self._lang])
                self.post_message({"type": "status", "text": tr("saved")})
                self.post_state()
            elif action == "check":
                info = api_get(load_settings(), "/api/v1/info")
                self.post_message(
                    {"type": "status", "text": tr("connected", version=info.get("version"), db=info.get("db_type"))}
                )
            elif action == "bind":
                self.bind(data)
            elif action == "unbind":
                profile = data["profile"]
                unbind_profile(profile)
                self.post_message({"type": "status", "text": tr("unbound", preset=short_name(profile))})
                self.post_state()
            elif action == "sync":
                self.sync()
            else:
                self.post_message({"type": "status", "text": tr("unknown", action=action), "error": True})
        except Exception as exc:  # noqa: BLE001
            log(f"{action} failed: {type(exc).__name__}: {exc}")
            self.post_message({"type": "status", "text": f"{type(exc).__name__}: {exc}", "error": True})

    def post_state(self):
        self.post_message({"type": "state", "payload": state_payload(STRINGS[self._lang])})

    def bind(self, data):
        profile = data.get("profile")
        if not profile:
            raise ValueError(self.tr("no_preset"))
        spool_id = data.get("spool_id")
        if spool_id is None:
            unbind_profile(profile)
            self.post_message({"type": "status", "text": self.tr("unbound", preset=short_name(profile))})
            self.post_state()
            return
        spools = {spool["id"]: spool for spool in fetch_spools(load_settings())}
        spool = spools.get(int(spool_id))
        if spool is None:
            raise ValueError(self.tr("spool_missing", id=spool_id))
        bind_profile(profile, spool)
        self.post_message(
            {
                "type": "status",
                "text": self.tr("bound", preset=short_name(profile), id=spool["id"], spool=spool_alias(spool)),
            }
        )
        self.post_state()

    def sync(self):
        settings = load_settings()
        spools = fetch_spools(settings)
        profiles = all_filaments()
        bindings = load_map()
        bound_ids = {record["spool_id"] for record in bindings.values()}
        bound_profiles = set(bindings)
        matched, created = [], []
        for spool in spools:
            if spool["id"] in bound_ids:
                continue
            match = best_match(spool, profiles)
            if match is not None and match["name"] not in bound_profiles:
                bind_profile(match["name"], spool)
                bound_profiles.add(match["name"])
                bound_ids.add(spool["id"])
                matched.append(f"#{spool['id']} → {match['label']}")
                continue
            parent = material_parent(spool["material"], spool, profiles)
            name = write_bundle_preset(spool, parent)
            bind_profile(name, spool)
            bound_ids.add(spool["id"])
            created.append(f"#{spool['id']} {spool_alias(spool)}")
        error = reload_bundle() if created else None
        parts = []
        if matched:
            parts.append(self.tr("matched", list="; ".join(matched)))
        if created:
            parts.append(self.tr("created", list=", ".join(created)))
        text = "; ".join(parts) or self.tr("all_matched")
        if error:
            text += self.tr("reload_failed", error=error)
        self.post_message({"type": "status", "text": text, "error": bool(error)})
        self.post_state()


class SpoolmanGCode(orca.slicing.SlicingPipelineCapabilityBase):
    def get_name(self):
        return "Spoolman G-code"

    def execute(self, ctx):
        try:
            if ctx.step != orca.slicing.Step.psGCodePostProcess:
                return orca.ExecutionResult.success()
            path = ctx.gcode_path
            if not path:
                return orca.ExecutionResult.success()
            settings = load_settings()
            names = filament_names_from_ctx(ctx)
            bindings = load_map()
            log(f"post-process {os.path.basename(path)}, filaments: {names}")
            for name in names:
                record = bindings.get(name)
                if record is None:
                    log(f"no binding for {name!r}")
                    continue
                line = macro_line(settings, record["spool_id"])
                if inject_macro(path, line):
                    return orca.ExecutionResult.success(f"G-code updated: {line}")
                return orca.ExecutionResult.success()
            return orca.ExecutionResult.skipped("no spool bound to the active filament")
        except Exception as exc:  # noqa: BLE001
            log(f"post-process failed: {type(exc).__name__}: {exc}")
            return orca.ExecutionResult.failure(
                orca.PluginResult.RecoverableError, f"MAD-Spoolman: {type(exc).__name__}: {exc}"
            )


@orca.plugin
class MADSpoolman(orca.base):
    def register_capabilities(self):
        orca.register_capability(SpoolmanPage)
        orca.register_capability(SpoolmanGCode)
