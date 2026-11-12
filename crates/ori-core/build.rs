use std::{env, fs, path::PathBuf};

fn modern_files(directory: &std::path::Path, depth: usize, files: &mut Vec<PathBuf>) {
    assert!(depth <= 8, "modern asset directory depth");
    let mut entries = fs::read_dir(directory)
        .expect("modern asset directory")
        .map(|entry| entry.expect("modern asset entry"))
        .collect::<Vec<_>>();
    entries.sort_by_key(|entry| entry.file_name());
    for entry in entries {
        let kind = entry.file_type().expect("modern asset type");
        assert!(!kind.is_symlink(), "linked modern assets are refused");
        let name = entry
            .file_name()
            .into_string()
            .expect("UTF-8 modern asset name");
        assert!(
            !name.starts_with('.')
                && name
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || b"-_.".contains(&c)),
            "unsafe modern asset name"
        );
        if kind.is_dir() {
            modern_files(&entry.path(), depth + 1, files);
        } else {
            assert!(kind.is_file(), "regular modern assets are required");
            assert!(files.len() < 256, "modern asset count");
            files.push(entry.path());
        }
    }
}

fn mime(name: &str) -> Option<&'static str> {
    match name.rsplit('.').next() {
        Some("html") => Some("text/html; charset=utf-8"),
        Some("js") => Some("text/javascript; charset=utf-8"),
        Some("css") => Some("text/css; charset=utf-8"),
        Some("svg") => Some("image/svg+xml"),
        Some("png") => Some("image/png"),
        Some("woff2") => Some("font/woff2"),
        Some("ico") => Some("image/x-icon"),
        _ => None,
    }
}
fn main() {
    let manifest = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").expect("manifest directory"));
    let directory = manifest.join("../../radioinspector/web/static");
    println!("cargo:rerun-if-changed={}", directory.display());
    let mut files = fs::read_dir(&directory)
        .expect("shared web assets")
        .map(|entry| entry.expect("asset entry"))
        .collect::<Vec<_>>();
    files.sort_by_key(|entry| entry.file_name());
    let mut generated = String::from("pub(super) const ASSETS: &[(&str,&str,&[u8])] = &[\n");
    let mut total = 0;
    for entry in files {
        if !entry.file_type().expect("asset type").is_file() {
            continue;
        }
        let name = entry.file_name().into_string().expect("UTF-8 asset name");
        assert!(
            name.bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || b"-_.".contains(&byte)),
            "unsafe asset name"
        );
        let Some(mime) = mime(&name) else {
            continue;
        };
        let length = entry.metadata().expect("asset metadata").len();
        assert!(length <= 1024 * 1024, "individual asset budget");
        total += length;
        assert!(total <= 16 * 1024 * 1024, "combined asset budget");
        generated.push_str(&format!("({name:?},{mime:?},include_bytes!(concat!(env!(\"CARGO_MANIFEST_DIR\"),\"/../../radioinspector/web/static/{name}\"))),\n"));
    }
    let modern = manifest.join("../../radioinspector/web/modern");
    println!("cargo:rerun-if-changed={}", modern.display());
    if modern.exists() {
        assert!(
            fs::symlink_metadata(&modern)
                .expect("modern asset root")
                .is_dir(),
            "regular modern asset directory required"
        );
        assert!(
            modern.join("index.html").is_file() && modern.join("modern-build.json").is_file(),
            "package a completed modern Web build before compiling"
        );
        let mut files = Vec::new();
        modern_files(&modern, 0, &mut files);
        for path in files {
            let name = path
                .strip_prefix(&modern)
                .expect("modern asset relative path")
                .to_str()
                .expect("UTF-8 modern asset path")
                .replace('\\', "/");
            let Some(mime) = mime(&name) else {
                continue;
            };
            let length = fs::metadata(&path).expect("modern asset metadata").len();
            assert!(length <= 1024 * 1024, "individual modern asset budget");
            total += length;
            assert!(total <= 16 * 1024 * 1024, "combined asset budget");
            let public = format!("modern/{name}");
            generated.push_str(&format!("({public:?},{mime:?},include_bytes!(concat!(env!(\"CARGO_MANIFEST_DIR\"),\"/../../radioinspector/web/modern/{name}\"))),\n"));
        }
    }
    generated.push_str("];\n");
    fs::write(
        PathBuf::from(env::var_os("OUT_DIR").expect("build output")).join("assets.rs"),
        generated,
    )
    .expect("generated asset map");
}
