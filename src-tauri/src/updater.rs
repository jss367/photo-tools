use std::sync::atomic::{AtomicBool, Ordering};

use tauri::window::{ProgressBarState, ProgressBarStatus};
use tauri::{AppHandle, Manager};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_updater::UpdaterExt;

/// Prevents overlapping update checks from running simultaneously.
static CHECKING: AtomicBool = AtomicBool::new(false);

/// Spawn an update check on a background async task.
///
/// When `user_initiated` is true, a dialog is shown even when no update
/// is available or when the check fails. Background (automatic) checks
/// stay silent on "no update" and log errors without bothering the user.
///
/// If a user starts a check while another check or download is in progress,
/// tell them what is happening instead of making the menu item appear broken.
pub fn spawn_update_check(app: &AppHandle, user_initiated: bool) {
    // Atomically set the flag; if it was already true another check is running.
    if CHECKING.swap(true, Ordering::SeqCst) {
        log::debug!("Update check already in progress, skipping");
        if user_initiated {
            app.dialog()
                .message(
                    "Vireo is already checking for or downloading an update.\n\n\
                     You'll be notified when it is ready.",
                )
                .title("Update in Progress")
                .kind(MessageDialogKind::Info)
                .show(|_| {});
        }
        return;
    }

    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        match do_update_check(&handle, user_initiated).await {
            Ok(()) => {}
            Err(e) => {
                log::error!("Update check failed: {e}");
                if user_initiated {
                    handle
                        .dialog()
                        .message(format!("Could not check for updates:\n{e}"))
                        .title("Update Error")
                        .kind(MessageDialogKind::Error)
                        .show(|_| {});
                }
            }
        }
        CHECKING.store(false, Ordering::SeqCst);
    });
}

async fn do_update_check(
    app: &AppHandle,
    user_initiated: bool,
) -> Result<(), Box<dyn std::error::Error>> {
    let updater = app.updater()?;
    let update = match updater.check().await {
        Ok(u) => u,
        // A missing platform key in the manifest means the release pipeline
        // didn't publish an artifact for the current OS/arch (e.g. macOS
        // notarization failed mid-release). That's a broken release for the
        // developer to fix — not something to alarm end users about. Log
        // loudly so it shows up in vireo.log, then fall through to the
        // normal "no update available" path.
        Err(
            e @ (tauri_plugin_updater::Error::TargetNotFound(_)
            | tauri_plugin_updater::Error::TargetsNotFound(_)),
        ) => {
            log::error!("Update manifest missing platform entry: {e}");
            None
        }
        Err(e) => return Err(e.into()),
    };

    match update {
        Some(update) => {
            let version = update.version.clone();
            log::info!("Update available: v{version}");

            // Checking should give immediate feedback. Previously Vireo began
            // downloading the (often ~200 MB) package without showing anything
            // and only opened a dialog after installation had completed.
            let should_install = app
                .dialog()
                .message(format!(
                    "Vireo v{version} is available.\n\n\
                     Download and install it now? The download may take several minutes."
                ))
                .title("Update Available")
                .kind(MessageDialogKind::Info)
                .buttons(MessageDialogButtons::OkCancelCustom(
                    "Download & Install".into(),
                    "Later".into(),
                ))
                // This update check runs on a background task, so blocking the
                // task while the native dialog is open does not freeze the UI.
                .blocking_show();

            if !should_install {
                log::info!("Update v{version} deferred by user");
                return Ok(());
            }

            log::info!("Downloading update v{version}");
            set_update_progress(app, ProgressBarStatus::Indeterminate, Some(1));

            let progress_handle = app.clone();
            let finish_handle = app.clone();
            let mut downloaded = 0_u64;
            let mut last_progress = 0_u64;
            let install_result = update
                .download_and_install(
                    move |chunk_len, content_len| {
                        downloaded = downloaded.saturating_add(chunk_len as u64);
                        let Some(total) = content_len.filter(|total| *total > 0) else {
                            return;
                        };
                        let progress = downloaded.saturating_mul(100) / total;
                        if progress != last_progress {
                            last_progress = progress;
                            set_update_progress(
                                &progress_handle,
                                ProgressBarStatus::Normal,
                                Some(progress.min(100)),
                            );
                        }
                    },
                    move || {
                        log::info!("Update download complete; installing");
                        set_update_progress(
                            &finish_handle,
                            ProgressBarStatus::Indeterminate,
                            Some(100),
                        );
                    },
                )
                .await;
            clear_update_progress(app);

            if let Err(e) = install_result {
                log::error!("Update download or installation failed: {e}");
                app.dialog()
                    .message(format!("Could not download or install the update:\n{e}"))
                    .title("Update Error")
                    .kind(MessageDialogKind::Error)
                    .show(|_| {});
                return Ok(());
            }

            let handle = app.clone();
            app.dialog()
                .message(format!(
                    "Vireo v{version} has been downloaded.\n\nRestart now to update?"
                ))
                .title("Update Ready")
                .kind(MessageDialogKind::Info)
                .buttons(MessageDialogButtons::OkCancel)
                .show(move |restart| {
                    if restart {
                        handle.restart();
                    }
                });

            Ok(())
        }
        None => {
            log::info!("No update available");
            if user_initiated {
                app.dialog()
                    .message(format!(
                        "You're running the latest version of Vireo (v{}).",
                        env!("CARGO_PKG_VERSION")
                    ))
                    .title("Up to Date")
                    .kind(MessageDialogKind::Info)
                    .show(|_| {});
            }
            Ok(())
        }
    }
}

fn set_update_progress(app: &AppHandle, status: ProgressBarStatus, progress: Option<u64>) {
    if let Some(window) = app.get_webview_window("main") {
        if let Err(e) = window.set_progress_bar(ProgressBarState {
            status: Some(status),
            progress,
        }) {
            log::debug!("Could not update updater progress indicator: {e}");
        }
    }
}

fn clear_update_progress(app: &AppHandle) {
    set_update_progress(app, ProgressBarStatus::None, None);
}
