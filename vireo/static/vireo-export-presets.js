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
    try {
      var data = await safeFetch('/api/export/presets', undefined, {toast: false});
      if (data && Array.isArray(data.presets)) presets = data.presets;
    } catch (err) {
      // Keep the last known list; save/delete surface their own errors.
    }
    populateSelect();
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
    try {
      var data = await safeFetch('/api/export/presets', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: name, settings: collectSettings()}),
      }, {toast: false});
      if (data.error) throw new Error(data.error);
    } catch (err) {
      alert('Could not save preset: ' + err.message);
      return;
    }
    await refresh();
    presetSelect().value = SAVED_PREFIX + name;
    VireoViewPreferences.write(LAST_USED_KEY, SAVED_PREFIX + name);
    updateButtons();
    if (typeof showToast === 'function') {
      showToast('Saved export preset “' + name + '”', 'info');
    }
  }

  async function deleteSelected() {
    var name = selectedSavedName();
    if (!name) return;
    if (!window.confirm('Delete the export preset “' + name + '”?')) return;
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
    await refresh();
    presetSelect().value = 'custom';
    updateButtons();
    if (typeof showToast === 'function') {
      showToast('Deleted export preset “' + name + '”', 'info');
    }
  }

  /* Called by the host page after it resets the modal to defaults and
   * restores its view preferences: fetches the current preset list, then
   * re-applies whatever the user exported with last time. */
  async function modalOpened() {
    syncSubfolderNameState();
    await refresh();
    if (!overlay().classList.contains('open')) return;
    var last = VireoViewPreferences.read(LAST_USED_KEY);
    if (!last || last === 'custom') { updateButtons(); return; }
    var sel = presetSelect();
    if (last.indexOf(SAVED_PREFIX) === 0) {
      var preset = findPreset(last.slice(SAVED_PREFIX.length));
      if (preset) {
        sel.value = last;
        applySettings(preset.settings);
      }
    } else {
      sel.value = last;
      if (sel.value === last && typeof applyExportPreset === 'function') {
        applyExportPreset(last);
      }
    }
    updateButtons();
  }

  /* Flip the preset dropdown to "Custom" and remember that in the last-used
   * key. Hosts call this after mutating a modal field programmatically
   * (destination picker, template-variable buttons, …) because assigning
   * `.value` directly fires no input/change event, so the delegated listener
   * below would otherwise miss the edit and reopen the modal with the stale
   * preset. Same effect as the listener; kept as a public method so callers
   * are explicit rather than dispatching synthetic events. */
  function markCustom() {
    if (typeof markExportCustom === 'function') markExportCustom();
    VireoViewPreferences.write(LAST_USED_KEY, 'custom');
    // The combined "capture date & time" checkbox may carry a preset's exact
    // split (see applySettings). Once we're Custom, drop the hint so Browse
    // reverts to its default of sending both when the box is checked.
    var combined = document.getElementById('exportMetadataCaptureDateTime');
    if (combined) delete combined.dataset.presetFields;
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
