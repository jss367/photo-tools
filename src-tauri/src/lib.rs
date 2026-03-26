mod sidecar;

use sidecar::SidecarState;
use tauri::Manager;

#[tauri::command]
fn get_server_port(state: tauri::State<'_, SidecarState>) -> u16 {
    state.port
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            if cfg!(debug_assertions) {
                // In dev mode, don't spawn sidecar — developer runs Flask manually
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
                // Use a placeholder state pointing to the dev server
                app.manage(SidecarState {
                    child: std::sync::Mutex::new(None),
                    port: 8080,
                });
            } else {
                // Production: spawn the sidecar
                let state = sidecar::start_sidecar(app.handle())
                    .expect("Failed to start Python sidecar");
                let port = state.port;
                app.manage(state);

                // Update the main window URL to the sidecar's port
                if let Some(window) = app.get_webview_window("main") {
                    let url = format!("http://127.0.0.1:{}", port);
                    let _ = window.navigate(url.parse().unwrap());
                }
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                let app = window.app_handle();
                if let Some(state) = app.try_state::<SidecarState>() {
                    sidecar::stop_sidecar(&state);
                }
            }
        })
        .invoke_handler(tauri::generate_handler![get_server_port])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
