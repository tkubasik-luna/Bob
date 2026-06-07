use tauri::Manager;

mod aec_spike;

/// Toggle the visibility of the pre-declared debug window (PRD 0005).
///
/// The `debug` window is declared in `tauri.conf.json` with `visible: false`,
/// so it lives in the webview process from boot but is hidden until the user
/// presses `Cmd+Shift+D` in the Sphere window. `.show()` / `.hide()` preserve
/// the webview state across toggles — the same DOM, the same WS connection,
/// the same React component tree survive a hide/show cycle. That's why the
/// debug feed history persists when the user hides and re-opens the window.
#[tauri::command]
fn toggle_debug_window(app: tauri::AppHandle) -> Result<(), String> {
  let window = app
    .get_webview_window("debug")
    .ok_or_else(|| "debug window not found".to_string())?;
  let is_visible = window.is_visible().map_err(|e| e.to_string())?;
  if is_visible {
    window.hide().map_err(|e| e.to_string())?;
  } else {
    window.show().map_err(|e| e.to_string())?;
    // Bring the freshly-shown window above the Sphere so the user
    // doesn't have to alt-tab to it.
    let _ = window.set_focus();
  }
  Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
  tauri::Builder::default()
    .setup(|app| {
      if cfg!(debug_assertions) {
        app.handle().plugin(
          tauri_plugin_log::Builder::default()
            .level(log::LevelFilter::Info)
            .build(),
        )?;
      }
      Ok(())
    })
    .invoke_handler(tauri::generate_handler![
      toggle_debug_window,
      aec_spike::aec_spike_write_verdict,
      aec_spike::aec_spike_rust_fallback_available
    ])
    .run(tauri::generate_context!())
    .expect("error while running tauri application");
}
