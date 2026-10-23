//! One error type for every handler: a status and a short machine-readable code in JSON.

use axum::http::{header, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use serde_json::{json, Value};

#[derive(Debug)]
pub struct ApiError {
    pub status: StatusCode,
    pub code: &'static str,
    pub extra: Option<Value>,
    pub retry_after: Option<u64>,
}

impl ApiError {
    pub fn new(status: StatusCode, code: &'static str) -> ApiError {
        ApiError {
            status,
            code,
            extra: None,
            retry_after: None,
        }
    }

    pub fn with(mut self, extra: Value) -> ApiError {
        self.extra = Some(extra);
        self
    }

    pub fn bad(code: &'static str) -> ApiError {
        ApiError::new(StatusCode::BAD_REQUEST, code)
    }

    pub fn io(err: std::io::Error) -> ApiError {
        tracing::error!("storage error: {err}");
        ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, "storage_error")
    }
}

impl From<std::io::Error> for ApiError {
    fn from(err: std::io::Error) -> ApiError {
        ApiError::io(err)
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let mut body = json!({ "error": self.code });
        if let (Some(Value::Object(extra)), Some(map)) = (self.extra, body.as_object_mut()) {
            map.extend(extra);
        }
        let mut res = (self.status, axum::Json(body)).into_response();
        if let Some(secs) = self.retry_after {
            if let Ok(v) = HeaderValue::from_str(&secs.to_string()) {
                res.headers_mut().insert(header::RETRY_AFTER, v);
            }
        }
        res
    }
}
