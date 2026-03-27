mod menu;
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
        .plugin(tauri_plugin_dialog::init())
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
                match sidecar::start_sidecar(app.handle()) {
                    Ok(state) => {
                        let port = state.port;
                        app.manage(state);
                        if let Some(window) = app.get_webview_window("main") {
                            let url = format!("http://127.0.0.1:{}", port);
                            let _ = window.navigate(url.parse().unwrap());
                        }
                    }
                    Err(e) => {
                        log::error!("Failed to start sidecar: {}", e);
                        eprintln!("Vireo: Failed to start Python backend: {}", e);
                        std::process::exit(1);
                    }
                }
            }

            // Build and attach the native menu bar
            let menu = menu::build_menu(app.handle())?;
            app.set_menu(menu)?;

            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                if window.label() == "main" {
                    let app = window.app_handle();
                    if let Some(state) = app.try_state::<SidecarState>() {
                        sidecar::stop_sidecar(&state);
                    }
                }
            }
        })
        .on_menu_event(|app, event| {
            let id = event.id().0.as_str();

            // Navigation items — evaluate JS in the main webview
            if let Some(route) = menu::route_for_id(id) {
                if let Some(window) = app.get_webview_window("main") {
                    let js = format!("window.location.href = '{}'", route);
                    if let Err(e) = window.eval(&js) {
                        log::error!("Failed to navigate to {}: {}", route, e);
                    }
                }
            }
        })
        .invoke_handler(tauri::generate_handler![get_server_port])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
