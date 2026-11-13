//! Match the explicit interpreter supplied by the modernization runner.
pub fn program() -> std::path::PathBuf {
    std::env::var_os("ORI_TEST_PYTHON")
        .map(Into::into)
        .unwrap_or_else(|| "/usr/bin/python3".into())
}
