use serde::Serialize;
use tauri::AppHandle;
use tauri_plugin_updater::UpdaterExt;

const PLACEHOLDER_PUBKEY: &str = "REPLACE_WITH_PUBLIC_KEY_FROM_TASK_1";

#[derive(Serialize)]
pub struct UpdateInfo {
    pub available: bool,
    pub version: Option<String>,
    pub notes: Option<String>,
    pub date: Option<String>,
}

/// Check whether the updater pubkey is still the placeholder value.
/// Returns true if signing infrastructure has not been configured yet.
fn is_pubkey_placeholder(app: &AppHandle) -> bool {
    app.config()
        .plugins
        .0
        .get("updater")
        .and_then(|u| u.get("pubkey"))
        .and_then(|v| v.as_str())
        .map(|k| k == PLACEHOLDER_PUBKEY)
        .unwrap_or(true)
}

#[tauri::command]
pub async fn check_for_update(app: AppHandle) -> Result<UpdateInfo, String> {
    if is_pubkey_placeholder(&app) {
        log::warn!("Update checking not configured (signing key not set)");
        return Ok(UpdateInfo {
            available: false,
            version: None,
            notes: Some("Update checking not configured (signing key not set)".into()),
            date: None,
        });
    }

    let updater = app.updater().map_err(|e| e.to_string())?;
    match updater.check().await {
        Ok(Some(update)) => Ok(UpdateInfo {
            available: true,
            version: Some(update.version.clone()),
            notes: update.body.clone(),
            date: update.date.map(|d| d.to_string()),
        }),
        Ok(None) => Ok(UpdateInfo {
            available: false,
            version: None,
            notes: None,
            date: None,
        }),
        Err(e) => Err(format!("Update check failed: {}", e)),
    }
}

#[tauri::command]
pub async fn install_update(app: AppHandle) -> Result<(), String> {
    if is_pubkey_placeholder(&app) {
        return Err("Updater not configured (signing key not set)".into());
    }

    let updater = app.updater().map_err(|e| e.to_string())?;
    let update = updater
        .check()
        .await
        .map_err(|e| format!("Update check failed: {}", e))?
        .ok_or_else(|| "No update available".to_string())?;
    update
        .download_and_install(|_, _| {}, || {})
        .await
        .map_err(|e| format!("Update install failed: {}", e))?;
    Ok(())
}
