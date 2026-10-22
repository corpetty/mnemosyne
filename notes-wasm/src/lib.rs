//! hashseq's wasm bindings (`WasmHashWeb` and friends) for the notes editor, and notes-core's
//! markdown for the client.
//!
//! The `#[wasm_bindgen]` exports live in `hashseq::wasm`; re-exporting links
//! them into this cdylib, so wasm-bindgen generates JS for them.
use wasm_bindgen::prelude::*;

pub use hashseq::wasm::*;

/// Markdown to the client's `NotesModel` (JSON; see `notes_core::client`): the text with
/// U+001F between table cells, a kind per line, inline ranges in code points. Never fails:
/// anything is markdown.
#[wasm_bindgen(js_name = parseMarkdown)]
pub fn parse_markdown(markdown: &str) -> String {
    notes_core::client::parse_markdown(markdown)
}

/// A `NotesModel` (JSON) to canonical markdown, the same rendering the server's fold gives.
/// Throws a message when the JSON is not a model.
#[wasm_bindgen(js_name = renderMarkdown)]
pub fn render_markdown(model_json: &str) -> Result<String, JsError> {
    notes_core::client::render_json(model_json).map_err(|e| JsError::new(&e.0))
}
