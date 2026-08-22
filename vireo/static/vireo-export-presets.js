/* Saved export presets for the export modal (Browse and Photo Editor).
 *
 * Both host pages render the modal with identical control ids and define the
 * same helper globals (applyExportPreset, markExportCustom,
 * updateExportFormatControls, updateExportPreview,
 * selectedExportMetadataFields), so this shared file owns everything
 * preset-shaped: listing, applying, saving, deleting, and re-applying the
 * last-used preset when the modal opens. Controls that only exist on one
 * page (e.g. the reveal-after-export checkbox) are feature-detected.
 */
var VireoExportPresets = (function() {
  var SAVED_PREFIX = 'saved:';
  var LAST_USED_KEY = 'vireo.export.lastPreset';
  var DEFAULT_SUBFOLDER = 'exported';
  var presets = [];
  var builtinOptions = null;
  var modalEditGeneration = 0;
  var modalOpenGeneration = 0;
  var refreshGeneration = 0;

  function $(id) { return document.getElementById(id); }
  function overlay() { return $('exportOverlay'); }
  function presetSelect() { return $('exportPreset'); }

  function findPreset(name) {
    for (var i = 0; i < presets.length; i++) {
      if (presets[i].name === name) return presets[i];
    }
    return null;
  }

  function selectedSavedName() {
    var value = presetSelect().value;
    return value.indexOf(SAVED_PREFIX) === 0 ? value.slice(SAVED_PREFIX.length) : null;
  }

  function updateButtons() {
    var deleteBtn = $('exportPresetDeleteBtn');
    if (deleteBtn) deleteBtn.disabled = !selectedSavedName();
  }

  function subfolderName() {
    var input = $('exportSubfolderName');
    if (!input) return DEFAULT_SUBFOLDER;
    return input.value.trim() || DEFAULT_SUBFOLDER;
  }

  function syncSubfolderNameState() {
    var input = $('exportSubfolderName');
    var checkbox = $('exportSubfolder');
    if (input && checkbox) input.disabled = !checkbox.checked;
  }

  function captureBuiltins() {
    if (builtinOptions) return;
    builtinOptions = Array.prototype.map.call(presetSelect().options, function(option) {
      return {value: option.value, label: option.textContent};
    });
  }

  function populateSelect() {
    var sel = presetSelect();
    captureBuiltins();
    var current = sel.value;
    sel.replaceChildren();
    function appendOptions(parent, entries) {
      entries.forEach(function(entry) {
        var option = document.createElement('option');
        option.value = entry.value;
        option.textContent = entry.label;
        parent.appendChild(option);
      });
    }
    if (presets.length) {
      var savedGroup = document.createElement('optgroup');
      savedGroup.label = 'Saved';
      appendOptions(savedGroup, presets.map(function(preset) {
        return {value: SAVED_PREFIX + preset.name, label: preset.name};
      }));
      sel.appendChild(savedGroup);
      var builtinGroup = document.createElement('optgroup');
      builtinGroup.label = 'Built-in';
      appendOptions(builtinGroup, builtinOptions);
      sel.appendChild(builtinGroup);
    } else {
      appendOptions(sel, builtinOptions);
    }
    sel.value = current;
    if (sel.value !== current) sel.value = 'custom';
    updateButtons();
  }

  async function refresh() {
    var generation = ++refreshGeneration;
    try {
      var data = await safeFetch('/api/export/presets', undefined, {toast: false});
      if (generation !== refreshGeneration) return {current: false, ok: false};
      if (data && Array.isArray(data.presets)) presets = data.presets;
    } catch (err) {
      if (generation !== refreshGeneration) return {current: false, ok: false};
      // Keep the last known list; save/delete surface their own errors.
      populateSelect();
      return {current: true, ok: false};
    }
    populateSelect();
    return {current: true, ok: true};
  }

  function collectSettings() {
    var resizeValue = $('exportResize').value;
    var maxSize = null;
    if (resizeValue === 'custom') {
      maxSize = parseInt($('exportResizeCustom').value, 10) || null;
    } else if (resizeValue) {
      maxSize = parseInt(resizeValue, 10);
    }
    var reveal = $('exportRevealAfter');
    return {
      destination: $('exportDest').value.trim(),
      export_to_subfolder: $('exportSubfolder').checked,
      subfolder_name: subfolderName(),
      reveal_after_export: reveal ? reveal.checked : false,
      format: $('exportFormat').value || 'jpg',
      max_size: maxSize,
      quality: parseInt($('exportQuality').value, 10) || 92,
      naming_template: $('exportTemplate').value || '{original}',
      metadata_fields: typeof selectedExportMetadataFields === 'function'
        ? selectedExportMetadataFields() : [],
    };
  }

  function applySettings(settings) {
    settings = settings || {};
    $('exportDest').value = settings.destination || '';
    $('exportSubfolder').checked = !!settings.export_to_subfolder;
    var subName = $('exportSubfolderName');
    if (subName) subName.value = settings.subfolder_name || DEFAULT_SUBFOLDER;
    var reveal = $('exportRevealAfter');
    if (reveal) reveal.checked = !!settings.reveal_after_export;
    $('exportFormat').value = settings.format || 'jpg';
    var resizeSel = $('exportResize');
    var customInput = $('exportResizeCustom');
    var maxSize = settings.max_size || null;
    if (!maxSize) {
      resizeSel.value = '';
      customInput.value = '';
    } else {
      var asString = String(maxSize);
      var hasOption = Array.prototype.some.call(resizeSel.options, function(option) {
        return option.value === asString;
      });
      resizeSel.value = hasOption ? asString : 'custom';
      customInput.value = hasOption ? '' : asString;
    }
    customInput.style.display = resizeSel.value === 'custom' ? 'block' : 'none';
    var quality = settings.quality || 92;
    $('exportQuality').value = quality;
    $('exportQualityVal').textContent = String(quality);
    $('exportTemplate').value = settings.naming_template || '{original}';
    var fields = Array.isArray(settings.metadata_fields) ? settings.metadata_fields : [];
    document.querySelectorAll('#exportOverlay .export-metadata-option input').forEach(function(input) {
      if (input.value !== 'capture_date_time') {
        input.checked = fields.indexOf(input.value) !== -1;
        return;
      }
      // Browse renders capture date+time as one combined checkbox, but a
      // preset saved in Photo Editor may specify just one of the two. Check
      // the combined box when either is present, and stash the exact split
      // on the checkbox so selectedExportMetadataFields() can honor it
      // instead of silently sending both. Any user edit clears the stash
      // (see the modal-scoped input/change handler) so it reverts to the
      // Browse default of "both".
      var hasDate = fields.indexOf('capture_date') !== -1;
      var hasTime = fields.indexOf('capture_time') !== -1;
      input.checked = hasDate || hasTime;
      if (hasDate && !hasTime) {
        input.dataset.presetFields = 'capture_date';
      } else if (hasTime && !hasDate) {
        input.dataset.presetFields = 'capture_time';
      } else {
        delete input.dataset.presetFields;
      }
    });
    syncSubfolderNameState();
    if (typeof updateExportFormatControls === 'function') updateExportFormatControls();
    if (typeof updateExportPreview === 'function') updateExportPreview();
  }

  function onPresetChange() {
    modalEditGeneration++;
    var value = presetSelect().value;
    if (value.indexOf(SAVED_PREFIX) === 0) {
      var preset = findPreset(value.slice(SAVED_PREFIX.length));
      if (preset) applySettings(preset.settings);
    } else {
      // Built-in presets don't touch metadata_fields, so a preserved
      // date-only / time-only hint from a previously-applied saved preset
      // would silently carry over. Drop it when switching away.
      var combined = document.getElementById('exportMetadataCaptureDateTime');
      if (combined) delete combined.dataset.presetFields;
      if (typeof applyExportPreset === 'function') applyExportPreset(value);
    }
    VireoViewPreferences.write(LAST_USED_KEY, value);
    updateButtons();
  }

  async function saveCurrent() {
    var name = window.prompt('Save current export settings as preset:',
      selectedSavedName() || '');
    if (name === null) return;
    name = name.trim();
    if (!name) return;
    if (findPreset(name) &&
        !window.confirm('Replace the existing preset “' + name + '”?')) {
      return;
    }
    // Snapshot the modal edit AND open generations before the network round
    // trip. If the user tweaks any control while the POST + refresh are in
    // flight, the controls no longer match what got saved; if they close and
    // reopen the modal, modalOpened() has already restored Custom or another
    // preset. In either case keep the dropdown as-is instead of relabeling
    // the current fields as this preset.
    var editGeneration = modalEditGeneration;
    var openGeneration = modalOpenGeneration;
    var payload = collectSettings();
    try {
      var data = await safeFetch('/api/export/presets', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: name, settings: payload}),
      }, {toast: false});
      if (data.error) throw new Error(data.error);
    } catch (err) {
      alert('Could not save preset: ' + err.message);
      return;
    }
    var refreshResult = await refresh();
    var edited = editGeneration !== modalEditGeneration;
    var reopened = openGeneration !== modalOpenGeneration;
    var keptCurrent = edited || reopened ||
      !refreshResult.current || !refreshResult.ok;
    if (!keptCurrent) {
      presetSelect().value = SAVED_PREFIX + name;
      VireoViewPreferences.write(LAST_USED_KEY, SAVED_PREFIX + name);
    }
    updateButtons();
    if (typeof showToast === 'function') {
      var msg = keptCurrent
        ? 'Saved export preset “' + name + '” (kept current selection)'
        : 'Saved export preset “' + name + '”';
      showToast(msg, 'info');
    }
  }

  async function deleteSelected() {
    var name = selectedSavedName();
    if (!name) return;
    if (!window.confirm('Delete the export preset “' + name + '”?')) return;
    var editGeneration = modalEditGeneration;
    var openGeneration = modalOpenGeneration;
    try {
      var data = await safeFetch('/api/export/presets/' + encodeURIComponent(name), {
        method: 'DELETE',
      }, {toast: false});
      if (data.error) throw new Error(data.error);
    } catch (err) {
      alert('Could not delete preset: ' + err.message);
      return;
    }
    if (VireoViewPreferences.read(LAST_USED_KEY) === SAVED_PREFIX + name) {
      VireoViewPreferences.write(LAST_USED_KEY, 'custom');
    }
    var refreshResult = await refresh();
    var edited = editGeneration !== modalEditGeneration;
    var reopened = openGeneration !== modalOpenGeneration;
    if (refreshResult.current && !edited && !reopened) {
      presetSelect().value = 'custom';
      updateButtons();
    }
    if (typeof showToast === 'function') {
      showToast('Deleted export preset “' + name + '”', 'info');
    }
  }

  /* Called by the host page after it resets the modal to defaults and
   * restores its view preferences: fetches the current preset list, then
   * re-applies whatever the user exported with last time. */
  async function modalOpened() {
    var openGeneration = ++modalOpenGeneration;
    var editGeneration = modalEditGeneration;
    var restorationComplete = false;
    var submit = $('exportSubmitBtn');
    if (submit) submit.disabled = true;
    syncSubfolderNameState();
    try {
      var refreshResult = await refresh();
      if (!refreshResult.current) return;
      if (openGeneration !== modalOpenGeneration ||
          !overlay().classList.contains('open')) return;
      // Do not replace a destination, template, or other control the user
      // changed while the preset request was in flight.
      if (editGeneration !== modalEditGeneration) {
        restorationComplete = true;
        updateButtons();
        return;
      }
      var last = VireoViewPreferences.read(LAST_USED_KEY);
      if (!last || last === 'custom') {
        // A date-only or time-only preset applied earlier this session may
        // have left a presetFields hint on the combined capture-date+time
        // checkbox (see applySettings). The host has just reset the modal
        // to the "both" default; drop the stale split so
        // selectedExportMetadataFields() reflects that default instead of
        // silently exporting only one half.
        var combined = document.getElementById('exportMetadataCaptureDateTime');
        if (combined) delete combined.dataset.presetFields;
        // Host reset the selector to a built-in default before opening, but
        // VireoViewPreferences.restoreAll has since restored persisted
        // fields (subfolder, metadata boxes, reveal-after) that mark the
        // dialog Custom. Snap the dropdown back so it doesn't advertise a
        // built-in preset while the fields below no longer match it.
        if (last === 'custom') presetSelect().value = 'custom';
        restorationComplete = true;
        updateButtons();
        return;
      }
      var sel = presetSelect();
      if (last.indexOf(SAVED_PREFIX) === 0) {
        var preset = findPreset(last.slice(SAVED_PREFIX.length));
        if (!preset) {
          // A successful list proves the saved preset was removed; fall back
          // to Custom. A failed list cannot prove that, so keep Export gated
          // rather than silently using the host defaults in its place.
          if (refreshResult.ok) {
            VireoViewPreferences.write(LAST_USED_KEY, 'custom');
            sel.value = 'custom';
            restorationComplete = true;
            updateButtons();
          }
          return;
        }
        sel.value = last;
        applySettings(preset.settings);
      } else {
        sel.value = last;
        if (sel.value === last && typeof applyExportPreset === 'function') {
          applyExportPreset(last);
        }
      }
      restorationComplete = true;
      updateButtons();
    } finally {
      // The export button is the only submission path on both host pages.
      // Keep it gated until the latest modal opening has either restored the
      // preset or deliberately preserved an edit made during the request.
      if (submit && openGeneration === modalOpenGeneration &&
          overlay().classList.contains('open') && restorationComplete) {
        submit.disabled = false;
      }
    }
  }

  /* Flip the preset dropdown to "Custom" and remember that in the last-used
   * key. Hosts call this after mutating a modal field programmatically
   * (destination picker, template-variable buttons, …) because assigning
   * `.value` directly fires no input/change event, so the delegated listener
   * below would otherwise miss the edit and reopen the modal with the stale
   * preset. Same effect as the listener; kept as a public method so callers
   * are explicit rather than dispatching synthetic events.
   *
   * Note: the combined "capture date & time" checkbox's ``presetFields``
   * hint (see applySettings) is *not* cleared here. It only reflects the
   * split the user selected via the preset, so tweaks to unrelated fields
   * (quality, destination, template) must not silently promote a
   * date-only or time-only preset to both. The hint is dropped only when
   * the combined checkbox itself is edited (see the delegated listener
   * below) or when a different preset is applied (see onPresetChange /
   * applySettings). */
  function markCustom() {
    modalEditGeneration++;
    if (typeof markExportCustom === 'function') markExportCustom();
    VireoViewPreferences.write(LAST_USED_KEY, 'custom');
    updateButtons();
  }

  function init() {
    if (!overlay() || !presetSelect()) return;
    presetSelect().addEventListener('change', onPresetChange);
    var saveBtn = $('exportPresetSaveBtn');
    if (saveBtn) saveBtn.addEventListener('click', saveCurrent);
    var deleteBtn = $('exportPresetDeleteBtn');
    if (deleteBtn) deleteBtn.addEventListener('click', deleteSelected);
    // Any manual tweak flips the dropdown to "Custom": it must never keep
    // displaying a preset name once the fields below no longer match it.
    ['input', 'change'].forEach(function(type) {
      overlay().addEventListener(type, function(event) {
        var target = event.target;
        if (!target || target.id === 'exportPreset') return;
        if (!target.matches || !target.matches('input, select, textarea')) return;
        // The combined "capture date & time" checkbox may carry a preset's
        // exact date/time split (see applySettings). Only drop that hint
        // when the box itself is edited so unrelated tweaks (quality,
        // destination, template, other metadata boxes) don't silently
        // promote a date-only or time-only preset to sending both.
        if (target.id === 'exportMetadataCaptureDateTime') {
          delete target.dataset.presetFields;
        }
        markCustom();
        if (target.id === 'exportSubfolder') syncSubfolderNameState();
        if ((target.id === 'exportSubfolder' || target.id === 'exportSubfolderName') &&
            typeof updateExportPreview === 'function') {
          updateExportPreview();
        }
      });
    });
    refresh();
  }
  init();

  return {
    modalOpened: modalOpened,
    subfolderName: subfolderName,
    markCustom: markCustom,
  };
})();
