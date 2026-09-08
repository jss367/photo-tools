use std::sync::Mutex;

use tauri::window::{ProgressBarState, ProgressBarStatus};
use tauri::{AppHandle, Manager};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_updater::{Update, UpdaterExt};

struct PendingUpdate {
    update: Update,
    bytes: Vec<u8>,
}

/// Keep deferred downloads for this session, and serialize checks through the
/// restart decision so another check cannot download or prompt a second time.
struct UpdateState<T> {
    running: bool,
    user_visible: bool,
    pending: Option<T>,
}

impl<T> UpdateState<T> {
    const fn new() -> Self {
        Self {
            running: false,
            user_visible: false,
            pending: None,
        }
    }

    fn begin(&mut self, user_initiated: bool) -> bool {
        if self.running {
            self.user_visible |= user_initiated;
            return false;
        }
        // Once the user chooses Later, only an explicit menu request should
        // offer that download again. Background checks leave it ready.
        if self.pending.is_some() && !user_initiated {
            return false;
        }
        self.running = true;
        self.user_visible = user_initiated;
        true
    }

    fn finish(&mut self) -> bool {
        self.running = false;
        std::mem::take(&mut self.user_visible)
    }
}

static STATE: Mutex<UpdateState<PendingUpdate>> = Mutex::new(UpdateState::new());

/// Download in the background and ask once, when the update is ready to install.
/// Manual checks also report no-update and error results. Repeated clicks promote
/// an existing check to user-visible without opening another progress dialog.
pub fn spawn_update_check(app: &AppHandle, user_initiated: bool) {
    let pending = {
        let mut state = STATE.lock().unwrap();
        if !state.begin(user_initiated) {
            return;
        }
        state.pending.take()
    };

    set_update_menu_text(app, "Checking for Updates…");
    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        let result = match pending {
            Some(pending) => Ok(Some(pending)),
            None => download_update(&handle).await,
        };
        let result = match result {
            Ok(Some(pending)) => {
                offer_restart(&handle, pending);
                Ok(true)
            }
            Ok(None) => Ok(false),
            Err(error) => Err(error),
        };
        // Restore the menu before releasing the check guard. Never hold the
        // mutex during a native UI call, which may dispatch to the main thread.
        let ready = STATE.lock().unwrap().pending.is_some();
        set_update_menu_text(
            &handle,
            if ready {
                "Restart to Update…"
            } else {
                "Check for Updates..."
            },
        );
        let visible = STATE.lock().unwrap().finish();
        match result {
            Ok(false) if visible => {
                handle
                    .dialog()
                    .message(format!(
                        "You're running the latest version of Vireo (v{}).",
                        env!("CARGO_PKG_VERSION")
                    ))
                    .title("Up to Date")
                    .kind(MessageDialogKind::Info)
                    .show(|_| {});
            }
            Err(error) => {
                log::error!("Update check or download failed: {error}");
                if visible {
                    handle
                        .dialog()
                        .message(format!("Could not check for or download updates:\n{error}"))
                        .title("Update Error")
                        .kind(MessageDialogKind::Error)
                        .show(|_| {});
                }
            }
            _ => {}
        }
    });
}

async fn download_update(
    app: &AppHandle,
) -> Result<Option<PendingUpdate>, Box<dyn std::error::Error>> {
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

            log::info!("Downloading update v{version}");
            set_update_menu_text(app, "Downloading Update…");
            set_update_progress(app, ProgressBarStatus::Indeterminate, Some(10));

            let progress_handle = app.clone();
            let finish_handle = app.clone();
            let mut downloaded = 0_u64;
            let mut last_progress = 0_u64;
            let download_result = update
                .download(
                    move |chunk_len, content_len| {
                        downloaded = downloaded.saturating_add(chunk_len as u64);
                        let Some(total) = content_len.filter(|total| *total > 0) else {
                            return;
                        };
                        let progress = downloaded.saturating_mul(100) / total;
                        if progress != last_progress {
                            last_progress = progress;
                            set_update_menu_text(
                                &progress_handle,
                                &format!("Downloading Update… {}%", progress.min(100)),
                            );
                            set_update_progress(
                                &progress_handle,
                                ProgressBarStatus::Normal,
                                Some(progress.min(100)),
                            );
                        }
                    },
                    move || {
                        log::info!("Update download complete; verifying");
                        set_update_menu_text(&finish_handle, "Verifying Update…");
                        set_update_progress(
                            &finish_handle,
                            ProgressBarStatus::Indeterminate,
                            Some(100),
                        );
                    },
                )
                .await;
            clear_update_progress(app);

            let bytes = download_result?;
            Ok(Some(PendingUpdate { update, bytes }))
        }
        None => {
            log::info!("No update available");
            Ok(None)
        }
    }
}

fn offer_restart(app: &AppHandle, pending: PendingUpdate) {
    set_update_menu_text(app, "Restart to Update…");
    // This runs on a background task. Holding the check guard through the
    // dialog prevents overlapping checks while leaving the UI responsive.
    let restart = app
        .dialog()
        .message(format!(
            "Vireo v{} has been downloaded and is ready to install.\n\n\
             Restart Vireo to finish updating, or choose Later to keep working.",
            pending.update.version
        ))
        .title("Update Ready")
        .kind(MessageDialogKind::Info)
        .buttons(MessageDialogButtons::OkCancelCustom(
            "Restart to Update".into(),
            "Later".into(),
        ))
        .blocking_show();

    if !restart {
        STATE.lock().unwrap().pending = Some(pending);
        return;
    }

    // Windows installation exits the process, so consent must come before
    // install() on every platform, not just before app.restart().
    set_update_menu_text(app, "Installing Update…");
    set_update_progress(app, ProgressBarStatus::Indeterminate, Some(100));
    let result = pending.update.install(&pending.bytes);
    clear_update_progress(app);
    match result {
        Ok(()) => app.restart(),
        Err(error) => {
            log::error!("Update installation failed: {error}");
            STATE.lock().unwrap().pending = Some(pending);
            app.dialog()
                .message(format!("Could not install the update:\n{error}"))
                .title("Update Error")
                .kind(MessageDialogKind::Error)
                .show(|_| {});
        }
    }
}

fn set_update_menu_text(app: &AppHandle, text: &str) {
    // Menu::get only searches direct children; the updater lives in Help.
    if let Some(item) = app
        .menu()
        .and_then(|menu| menu.items().ok())
        .and_then(|items| {
            items.into_iter().find_map(|item| {
                item.as_submenu()
                    .and_then(|submenu| submenu.get(crate::menu::ids::CHECK_FOR_UPDATES))
            })
        })
    {
        if let Some(item) = item.as_menuitem() {
            if let Err(error) = item.set_text(text) {
                log::debug!("Could not update updater menu status: {error}");
            }
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

#[cfg(test)]
mod tests {
    use super::UpdateState;

    #[test]
    fn manual_request_promotes_background_check_without_starting_another() {
        let mut state = UpdateState::<()>::new();
        assert!(state.begin(false));
        assert!(!state.begin(true));
        assert!(!state.begin(false));
        assert!(state.finish());

        // Visibility belongs to this check, not the next background check.
        assert!(state.begin(false));
        assert!(!state.finish());
    }

    #[test]
    fn deferred_download_waits_for_manual_request_and_is_reused() {
        let mut state = UpdateState::new();
        assert!(state.begin(false));
        state.pending = Some(vec![1, 2, 3]);
        state.finish();

        // Scheduled checks must neither prompt again nor redownload.
        assert!(!state.begin(false));
        assert_eq!(state.pending.as_deref(), Some([1, 2, 3].as_slice()));

        assert!(state.begin(true));
        assert_eq!(state.pending.take(), Some(vec![1, 2, 3]));
        // The guard also covers the restart prompt and installation.
        assert!(!state.begin(true));
        assert!(!state.begin(false));
        assert!(state.finish());
    }

    #[test]
    fn completed_check_allows_retry() {
        let mut state = UpdateState::<()>::new();
        assert!(state.begin(true));
        assert!(state.finish());
        assert!(state.begin(true));
        assert!(state.finish());
    }
}
