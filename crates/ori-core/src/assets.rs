//! Immutable shared program assets embedded at build time, never runtime paths.
use axum::{
    body::Body,
    http::{StatusCode, header},
    response::Response,
};
include!(concat!(env!("OUT_DIR"), "/assets.rs"));
pub(crate) fn contains(path: &str) -> bool {
    find(path).is_some()
}
pub(crate) fn modern_available() -> bool {
    ASSETS
        .iter()
        .any(|(name, _, _)| *name == "modern/index.html")
}
fn find(path: &str) -> Option<(&'static str, &'static [u8])> {
    let name = match path {
        "/" | "/index.html" => "index.html",
        "/platform" | "/platform.html" => "platform.html",
        "/instrument" | "/instrument.html" => "instrument.html",
        "/modern" | "/modern/" => "modern/index.html",
        _ if path.starts_with("/modern/") => path.strip_prefix('/')?,
        _ => path.strip_prefix("/static/")?,
    };
    ASSETS
        .iter()
        .find(|(candidate, _, _)| *candidate == name)
        .map(|(_, mime, bytes)| (*mime, *bytes))
}
pub(crate) fn response(path: &str) -> Option<Response> {
    if matches!(path, "/modern" | "/modern/") && !modern_available() {
        return Some(
            Response::builder()
                .status(StatusCode::SERVICE_UNAVAILABLE)
                .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
                .header(header::CACHE_CONTROL, "no-store")
                .header("X-Content-Type-Options", "nosniff")
                .body(Body::from(
                    "Modern client assets are not built; the legacy UI is available at /",
                ))
                .expect("static asset headers"),
        );
    }
    let (mime, bytes) = find(path)?;
    Some(
        Response::builder()
            .status(StatusCode::OK)
            .header(header::CONTENT_TYPE, mime)
            .header(header::CONTENT_LENGTH, bytes.len())
            .header(header::CACHE_CONTROL, "no-store")
            .header("X-Content-Type-Options", "nosniff")
            .body(Body::from(bytes))
            .expect("static asset headers"),
    )
}
