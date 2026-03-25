# Multi-Model Classification & Compare View — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Allow selecting multiple models on the pipeline page (each spawns an independent classification job) and view predictions side-by-side on a new Compare page.

**Architecture:** Replace the single model `<select>` with checkboxes per downloaded model. "Classify" fires one POST per selected model to `/api/jobs/classify`. A new `/compare` page fetches all predictions for a collection grouped by photo and pivoted by model, displayed in a table with disagreement highlighting.

**Tech Stack:** Flask, Jinja2, vanilla JS, SQLite (existing stack — no new dependencies)

---

### Task 1: Add `/api/predictions/compare` endpoint

This API returns predictions for a collection, organized for side-by-side comparison.

**Files:**
- Modify: `vireo/app.py` (after the existing `/api/predictions` route, ~line 994)
- Test: `vireo/tests/test_app.py`

**Step 1: Write the failing test**

Add to `vireo/tests/test_app.py`:

```python
def test_compare_predictions_api(app_and_db):
    """GET /api/predictions/compare returns per-photo, per-model data."""
    app, db = app_and_db

    # Create a collection and add photos
    cid = db.add_collection("Test Collection", "[]")
    photos = db.get_all_photos()
    for p in photos:
        db.add_photo_to_collection(cid, p["id"])

    # Add predictions from two models
    db.add_prediction(photos[0]["id"], "Cardinal", 0.95, "model-a")
    db.add_prediction(photos[0]["id"], "Blue Jay", 0.80, "model-b")
    db.add_prediction(photos[1]["id"], "Sparrow", 0.90, "model-a")
    db.add_prediction(photos[1]["id"], "Sparrow", 0.88, "model-b")

    client = app.test_client()
    resp = client.get(f"/api/predictions/compare?collection_id={cid}")
    assert resp.status_code == 200
    data = resp.get_json()

    assert "models" in data
    assert set(data["models"]) == {"model-a", "model-b"}
    assert "photos" in data
    assert len(data["photos"]) >= 2

    # Check structure of a photo entry
    photo = data["photos"][0]
    assert "photo_id" in photo
    assert "filename" in photo
    assert "predictions" in photo
    assert isinstance(photo["predictions"], dict)  # keyed by model name


def test_compare_predictions_api_requires_collection(app_and_db):
    """GET /api/predictions/compare without collection_id returns 400."""
    app, _ = app_and_db
    client = app.test_client()
    resp = client.get("/api/predictions/compare")
    assert resp.status_code == 400
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/julius/git/vireo/.worktrees/multi-model-compare && python -m pytest vireo/tests/test_app.py::test_compare_predictions_api vireo/tests/test_app.py::test_compare_predictions_api_requires_collection -v`
Expected: FAIL (404 — route doesn't exist)

**Step 3: Write the endpoint**

Add to `vireo/app.py` after the `api_predictions()` route (~line 994):

```python
@app.route("/api/predictions/compare")
def api_predictions_compare():
    db = _get_db()
    collection_id = request.args.get("collection_id", None, type=int)
    if not collection_id:
        return jsonify({"error": "collection_id required"}), 400

    photos = db.get_collection_photos(collection_id, per_page=999999)
    photo_ids = [p["id"] for p in photos]
    if not photo_ids:
        return jsonify({"models": [], "photos": []})

    preds = db.get_predictions(photo_ids=photo_ids)

    # Collect distinct models and build per-photo lookup
    models = set()
    by_photo = {}
    for pr in preds:
        d = dict(pr)
        pid = d["photo_id"]
        model = d["model"]
        models.add(model)
        if pid not in by_photo:
            by_photo[pid] = {"photo_id": pid, "filename": d["filename"], "predictions": {}}
        by_photo[pid]["predictions"][model] = {
            "species": d["species"],
            "confidence": d["confidence"],
        }

    return jsonify({
        "models": sorted(models),
        "photos": list(by_photo.values()),
    })
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/julius/git/vireo/.worktrees/multi-model-compare && python -m pytest vireo/tests/test_app.py::test_compare_predictions_api vireo/tests/test_app.py::test_compare_predictions_api_requires_collection -v`
Expected: PASS

**Step 5: Commit**

```bash
cd /Users/julius/git/vireo/.worktrees/multi-model-compare
git add vireo/app.py vireo/tests/test_app.py
git commit -m "feat: add /api/predictions/compare endpoint for multi-model comparison"
```

---

### Task 2: Add `/compare` page route and template

**Files:**
- Modify: `vireo/app.py` (~line 210, page routes section)
- Create: `vireo/templates/compare.html`
- Test: `vireo/tests/test_app.py`

**Step 1: Write the failing test**

Add to `vireo/tests/test_app.py`:

```python
def test_compare_page(app_and_db):
    """GET /compare returns 200."""
    app, _ = app_and_db
    client = app.test_client()
    resp = client.get('/compare')
    assert resp.status_code == 200
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/julius/git/vireo/.worktrees/multi-model-compare && python -m pytest vireo/tests/test_app.py::test_compare_page -v`
Expected: FAIL (404)

**Step 3: Add the route**

Add to `vireo/app.py` in the page routes section (~after line 217):

```python
@app.route("/compare")
def compare():
    return render_template("compare.html")
```

**Step 4: Create the template**

Create `vireo/templates/compare.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="icon" type="image/png" href="/favicon.ico">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<link rel="stylesheet" href="/static/vireo-base.css">
<title>Vireo - Compare Models</title>
<style>
body { padding-bottom: 36px; }
.content { max-width: 1200px; }

.compare-bar {
  background: var(--bg-secondary);
  border: 1px solid var(--border-primary);
  border-radius: 8px;
  padding: 12px 16px;
  margin-bottom: 16px;
  display: flex;
  align-items: center;
  gap: 12px;
}
.compare-bar label { font-size: 13px; color: var(--text-secondary); }
.compare-bar select {
  background: var(--bg-input); color: var(--text-primary);
  border: 1px solid var(--border-primary); border-radius: 4px;
  padding: 4px 8px; font-size: 13px;
}

.compare-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.compare-table th {
  background: var(--bg-tertiary);
  color: var(--text-secondary);
  padding: 8px 12px;
  text-align: left;
  border-bottom: 1px solid var(--border-primary);
  position: sticky;
  top: 0;
  z-index: 1;
}
.compare-table td {
  padding: 6px 12px;
  border-bottom: 1px solid var(--border-subtle);
  vertical-align: middle;
}
.compare-table tr:hover { background: var(--bg-tertiary); }
.compare-table tr.disagree { background: rgba(240, 192, 64, 0.08); }
.compare-table tr.disagree:hover { background: rgba(240, 192, 64, 0.14); }

.thumb-cell {
  display: flex;
  align-items: center;
  gap: 8px;
}
.thumb-cell img {
  width: 40px; height: 40px;
  object-fit: cover;
  border-radius: 4px;
}
.conf { color: var(--text-muted); font-size: 11px; }
.empty-cell { color: var(--text-ghost); }

.compare-empty {
  text-align: center;
  padding: 48px 16px;
  color: var(--text-muted);
}
</style>
</head>
<body>

{% set active_page = 'compare' %}
{% include '_navbar.html' %}

<div class="content">
  <div class="compare-bar">
    <label>Collection:</label>
    <select id="collectionPicker" onchange="loadComparison()">
      <option value="">Select collection...</option>
    </select>
    <span id="compareStatus" style="font-size:12px;color:var(--text-muted);"></span>
  </div>

  <div id="compareContent">
    <div class="compare-empty">Select a collection to compare model predictions.</div>
  </div>
</div>

<script>
async function loadCollections() {
  try {
    var resp = await fetch('/api/collections');
    var data = await resp.json();
    var sel = document.getElementById('collectionPicker');
    data.forEach(function(c) {
      var opt = document.createElement('option');
      opt.value = c.id;
      opt.textContent = c.name;
      sel.appendChild(opt);
    });
  } catch(e) { console.error('loadCollections error:', e); }
}

async function loadComparison() {
  var collId = document.getElementById('collectionPicker').value;
  var content = document.getElementById('compareContent');
  var status = document.getElementById('compareStatus');

  if (!collId) {
    content.innerHTML = '<div class="compare-empty">Select a collection to compare model predictions.</div>';
    return;
  }

  status.textContent = 'Loading...';
  try {
    var resp = await fetch('/api/predictions/compare?collection_id=' + collId);
    var data = await resp.json();
    status.textContent = '';

    if (data.models.length === 0) {
      content.innerHTML = '<div class="compare-empty">No predictions found for this collection. Run classification first.</div>';
      return;
    }

    if (data.models.length < 2) {
      content.innerHTML = '<div class="compare-empty">Only one model has predictions. Run classification with at least two models to compare.</div>';
      return;
    }

    renderTable(data);
  } catch(e) {
    status.textContent = 'Error loading data';
    console.error('loadComparison error:', e);
  }
}

function renderTable(data) {
  var content = document.getElementById('compareContent');
  var html = '<table class="compare-table"><thead><tr>';
  html += '<th>Photo</th>';
  data.models.forEach(function(m) {
    html += '<th>' + escHtml(m) + '</th>';
  });
  html += '</tr></thead><tbody>';

  data.photos.forEach(function(photo) {
    // Determine if models disagree
    var species = new Set();
    data.models.forEach(function(m) {
      var pred = photo.predictions[m];
      if (pred) species.add(pred.species);
    });
    var disagree = species.size > 1;

    html += '<tr class="' + (disagree ? 'disagree' : '') + '">';
    html += '<td><div class="thumb-cell">';
    html += '<img src="/api/thumbnails/' + photo.photo_id + '" alt="">';
    html += '<span>' + escHtml(photo.filename) + '</span>';
    html += '</div></td>';

    data.models.forEach(function(m) {
      var pred = photo.predictions[m];
      if (pred) {
        html += '<td>' + escHtml(pred.species);
        html += ' <span class="conf">' + Math.round(pred.confidence * 100) + '%</span></td>';
      } else {
        html += '<td class="empty-cell">&mdash;</td>';
      }
    });

    html += '</tr>';
  });

  html += '</tbody></table>';
  content.innerHTML = html;
}

function escHtml(s) {
  var d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

loadCollections();
</script>
</body>
</html>
```

**Step 5: Run test to verify it passes**

Run: `cd /Users/julius/git/vireo/.worktrees/multi-model-compare && python -m pytest vireo/tests/test_app.py::test_compare_page -v`
Expected: PASS

**Step 6: Commit**

```bash
cd /Users/julius/git/vireo/.worktrees/multi-model-compare
git add vireo/app.py vireo/templates/compare.html vireo/tests/test_app.py
git commit -m "feat: add /compare page with side-by-side model prediction table"
```

---

### Task 3: Add Compare link to navbar

**Files:**
- Modify: `vireo/templates/_navbar.html` (~line 708, after the Audit link)
- Test: `vireo/tests/test_app.py`

**Step 1: Write the failing test**

Add to `vireo/tests/test_app.py`:

```python
def test_compare_link_in_navbar(app_and_db):
    """The navbar includes a link to /compare."""
    app, _ = app_and_db
    client = app.test_client()
    resp = client.get('/compare')
    assert b'/compare' in resp.data
    assert b'Compare' in resp.data
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/julius/git/vireo/.worktrees/multi-model-compare && python -m pytest vireo/tests/test_app.py::test_compare_link_in_navbar -v`
Expected: FAIL (no "Compare" link in navbar HTML)

**Step 3: Add the link**

In `vireo/templates/_navbar.html`, after line 708 (the Audit link), add:

```html
  <a href="/compare" {% if active_page == 'compare' %}class="active"{% endif %}>Compare</a>
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/julius/git/vireo/.worktrees/multi-model-compare && python -m pytest vireo/tests/test_app.py::test_compare_link_in_navbar -v`
Expected: PASS

**Step 5: Commit**

```bash
cd /Users/julius/git/vireo/.worktrees/multi-model-compare
git add vireo/templates/_navbar.html vireo/tests/test_app.py
git commit -m "feat: add Compare link to navbar"
```

---

### Task 4: Convert pipeline model selector to multi-select checkboxes

**Files:**
- Modify: `vireo/templates/pipeline.html` (HTML ~line 196-201 and JS ~lines 353-373, 458-564)
- Test: `vireo/tests/test_app.py`

**Step 1: Write the failing test**

Add to `vireo/tests/test_app.py`:

```python
def test_pipeline_has_model_checkboxes(app_and_db):
    """Pipeline page uses checkboxes for model selection, not a single select."""
    app, _ = app_and_db
    client = app.test_client()
    resp = client.get('/pipeline')
    assert resp.status_code == 200
    assert b'model-checkbox' in resp.data
    assert b'id="cfgModel"' not in resp.data  # old single select removed
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/julius/git/vireo/.worktrees/multi-model-compare && python -m pytest vireo/tests/test_app.py::test_pipeline_has_model_checkboxes -v`
Expected: FAIL (old `cfgModel` select still present)

**Step 3: Replace the model selector HTML**

In `vireo/templates/pipeline.html`, replace lines 196-201 (the model setting-item div):

Old:
```html
        <div class="setting-item">
          <div class="setting-label">Model</div>
          <select class="setting-select" id="cfgModel" onchange="updateReadiness()">
            <option value="">Loading models...</option>
          </select>
        </div>
```

New:
```html
        <div class="setting-item">
          <div class="setting-label">Models</div>
          <div id="modelPicker" style="font-size:12px;max-height:100px;overflow-y:auto;">Loading models...</div>
        </div>
```

**Step 4: Replace `loadModels()` JS**

Replace the `loadModels` function (lines 353-373):

```javascript
async function loadModels() {
  try {
    var resp = await fetch('/api/models');
    var data = await resp.json();
    var div = document.getElementById('modelPicker');
    div.innerHTML = '';
    var downloaded = data.models.filter(function(m) { return m.downloaded; });
    if (downloaded.length === 0) {
      div.innerHTML = '<span style="color:var(--text-muted);">No models downloaded — go to Settings</span>';
    } else {
      downloaded.forEach(function(m) {
        var label = document.createElement('label');
        label.style.cssText = 'display:block;cursor:pointer;padding:2px 0;';
        var cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.className = 'model-checkbox';
        cb.value = m.id;
        cb.dataset.name = m.name;
        cb.style.cssText = 'accent-color:var(--accent);margin-right:4px;';
        cb.onchange = updateReadiness;
        if (m.id === data.active_id) cb.checked = true;
        label.appendChild(cb);
        label.appendChild(document.createTextNode(m.name + ' (' + m.model_str + ')'));
        div.appendChild(label);
      });
    }
    updateReadiness();
  } catch(e) { console.error('loadModels error:', e); }
}
```

**Step 5: Replace `runClassify()` to loop over selected models**

Replace the `runClassify` function (lines 458-564):

```javascript
async function runClassify(reclassify) {
  var collId = document.getElementById('collectionPicker').value;
  if (!collId) return;

  var selectedModels = [];
  document.querySelectorAll('.model-checkbox:checked').forEach(function(cb) {
    selectedModels.push({id: cb.value, name: cb.dataset.name});
  });
  if (selectedModels.length === 0) {
    alert('Select at least one model.');
    return;
  }

  var reclass = reclassify || document.getElementById('chkReclassify').checked;
  if (reclass && !confirm('This will clear existing predictions for selected models and re-classify. Continue?')) {
    return;
  }

  var btn = document.getElementById('btnClassify');
  var status = document.getElementById('statusClassify');
  var progressWrap = document.getElementById('progressClassify');
  var fill = document.getElementById('fillClassify');
  var text = document.getElementById('textClassify');
  var threshold = parseInt(document.getElementById('cfgThreshold').value, 10) / 100;
  var num = document.getElementById('numClassify');

  btn.disabled = true;
  num.className = 'stage-num running';
  progressWrap.style.display = '';
  fill.style.width = '0%';

  var labelsFiles = (function() {
    var files = [];
    document.querySelectorAll('.labels-run-cb:checked').forEach(function(cb) { files.push(cb.value); });
    return files.length > 0 ? files : undefined;
  })();

  var completed = 0;
  var total = selectedModels.length;

  for (var i = 0; i < selectedModels.length; i++) {
    var model = selectedModels[i];
    status.textContent = (total > 1 ? 'Model ' + (i+1) + '/' + total + ': ' : '') + 'Starting ' + model.name + '...';

    try {
      var resp = await fetch('/api/jobs/classify', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          collection_id: parseInt(collId),
          threshold: threshold,
          model_id: model.id,
          labels_files: labelsFiles,
          reclassify: !!reclass,
        }),
      });
      if (!resp.ok) {
        var err = await resp.json();
        status.textContent = 'Error (' + model.name + '): ' + (err.error || 'failed');
        continue;
      }
      var data = await resp.json();

      // Stream progress for this model's job
      await new Promise(function(resolve) {
        var source = new EventSource('/api/jobs/' + data.job_id + '/stream');
        source.addEventListener('progress', function(e) {
          var p = JSON.parse(e.data);
          // Progress within this model: scale to fraction of total
          var modelPct = p.total > 0 ? p.current / p.total : 0;
          var overallPct = Math.round(((completed + modelPct) / total) * 100);
          fill.style.width = overallPct + '%';
          var parts = [];
          if (total > 1) parts.push(model.name);
          if (p.phase) parts.push(p.phase);
          if (p.total > 0 && p.current > 0) parts.push(p.current + '/' + p.total);
          if (p.current_file) parts.push(p.current_file);
          text.textContent = parts.join(' — ');
          status.textContent = (total > 1 ? 'Model ' + (i+1) + '/' + total + ': ' : '') + parts.slice(total > 1 ? 1 : 0).join(' — ');
        });
        source.addEventListener('complete', function(e) {
          source.close();
          window.dispatchEvent(new CustomEvent('vireo-job-done', {detail: {job_id: data.job_id}}));
          completed++;
          resolve();
        });
        source.addEventListener('error_event', function(e) {
          source.close();
          completed++;
          resolve();
        });
        source.onerror = function() {
          source.close();
          completed++;
          resolve();
        };
      });
    } catch(e) {
      status.textContent = 'Error (' + model.name + '): ' + e.message;
    }
  }

  // All models done
  fill.style.width = '100%';
  text.textContent = 'Complete';
  status.textContent = 'Done! ' + completed + '/' + total + ' models classified.';
  status.className = 'status-msg ok';
  num.className = 'stage-num complete';
  btn.disabled = false;
  _pipelineState.hasDetections = true;
  markStale('extract');
  markStale('group');
  updateCardStates();
}
```

**Step 6: Update `updateReadiness()` to read from checkboxes**

Find where `updateReadiness` reads the model value — it likely references `cfgModel`. Update it to use the first checked model checkbox instead:

Search for `cfgModel` references in `updateReadiness` and replace `document.getElementById('cfgModel').value` with:

```javascript
(document.querySelector('.model-checkbox:checked') || {}).value || ''
```

**Step 7: Run test to verify it passes**

Run: `cd /Users/julius/git/vireo/.worktrees/multi-model-compare && python -m pytest vireo/tests/test_app.py::test_pipeline_has_model_checkboxes -v`
Expected: PASS

**Step 8: Run full test suite**

Run: `cd /Users/julius/git/vireo/.worktrees/multi-model-compare && python -m pytest tests/test_workspaces.py vireo/tests/test_db.py vireo/tests/test_app.py vireo/tests/test_photos_api.py vireo/tests/test_edits_api.py vireo/tests/test_jobs_api.py vireo/tests/test_darktable_api.py vireo/tests/test_config.py -v`
Expected: All 121+ tests pass

**Step 9: Commit**

```bash
cd /Users/julius/git/vireo/.worktrees/multi-model-compare
git add vireo/templates/pipeline.html vireo/tests/test_app.py
git commit -m "feat: convert pipeline model selector to multi-select checkboxes"
```

---

### Task 5: Run full test suite and create PR

**Step 1: Run full tests**

Run: `cd /Users/julius/git/vireo/.worktrees/multi-model-compare && python -m pytest tests/test_workspaces.py vireo/tests/test_db.py vireo/tests/test_app.py vireo/tests/test_photos_api.py vireo/tests/test_edits_api.py vireo/tests/test_jobs_api.py vireo/tests/test_darktable_api.py vireo/tests/test_config.py -v`
Expected: All tests pass

**Step 2: Push and create PR**

```bash
cd /Users/julius/git/vireo/.worktrees/multi-model-compare
git push -u origin feature/multi-model-compare
gh pr create --title "Multi-model classification and compare view" --body "$(cat <<'EOF'
## Summary
- Pipeline page model selector is now multi-select (checkboxes) — selecting multiple models spawns one classification job per model
- New `/compare` page shows predictions side-by-side in a table: rows are photos, columns are models
- Disagreement rows (where models predict different species) are highlighted in amber
- New `/api/predictions/compare` endpoint returns predictions grouped by photo and pivoted by model

## Test plan
- [ ] Run classification with one model — verify it works as before
- [ ] Run classification with two models — verify both jobs run sequentially and complete
- [ ] Visit /compare, select collection — verify table shows both models' predictions
- [ ] Verify disagreement rows are highlighted, agreement rows are plain
- [ ] Verify cells show dash for photos not classified by a model

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
