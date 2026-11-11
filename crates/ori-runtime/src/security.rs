//! LAN request policy, short-lived one-time pairing, and hash-only device credentials.
//! Route adapters must call BOTH `AccessPolicy::check` and `Credentials::authorize`.
use crate::{
    Error, Result,
    lease::{Lease, atomic_private, private_directory, read_private},
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use rand::RngCore;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeSet,
    path::{Path, PathBuf},
};
use subtle::ConstantTimeEq;
use url::Url;

pub(crate) fn random_secret() -> String {
    let mut bytes = [0u8; 32];
    rand::rngs::OsRng.fill_bytes(&mut bytes);
    URL_SAFE_NO_PAD.encode(bytes)
}
fn digest(secret: &str) -> String {
    URL_SAFE_NO_PAD.encode(Sha256::digest(secret.as_bytes()))
}
fn equal(left: &str, right: &str) -> bool {
    bool::from(left.as_bytes().ct_eq(right.as_bytes()))
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Record {
    hash: String,
    label: String,
    created_at_unix: u64,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Store {
    version: u32,
    tokens: Vec<Record>,
}

pub struct Credentials {
    directory: PathBuf,
}
impl Credentials {
    /// Caller selects an owner-only directory. Tokens are returned exactly once and never persisted.
    pub fn open(path: &Path) -> Result<Self> {
        private_directory(path)?;
        let result = Self {
            directory: path.to_owned(),
        };
        let _lock = result.lock()?;
        if !path.join("credentials.json").exists() {
            result.save(&Store {
                version: 1,
                tokens: vec![],
            })?;
        }
        result.load()?;
        Ok(result)
    }
    fn lock(&self) -> Result<Lease> {
        Lease::acquire(&self.directory.join("credentials.lock"))
    }
    fn load(&self) -> Result<Store> {
        let store: Store = serde_json::from_slice(&read_private(
            &self.directory.join("credentials.json"),
            1024 * 1024,
        )?)?;
        if store.version != 1 || store.tokens.len() > 256 {
            return Err(Error::Unsafe("unsupported credential store".into()));
        }
        Ok(store)
    }
    fn save(&self, store: &Store) -> Result<()> {
        atomic_private(
            &self.directory.join("credentials.json"),
            &serde_json::to_vec(store)?,
        )
    }
    pub fn issue(&mut self, label: &str, now: u64) -> Result<String> {
        if label.is_empty() || label.len() > 128 || label.chars().any(char::is_control) {
            return Err(Error::Unsafe("invalid device label".into()));
        }
        let _lock = self.lock()?;
        let mut store = self.load()?;
        if store.tokens.len() >= 256 {
            return Err(Error::Unsafe("device credential limit reached".into()));
        }
        let token = random_secret();
        store.tokens.push(Record {
            hash: digest(&token),
            label: label.into(),
            created_at_unix: now,
        });
        self.save(&store)?;
        Ok(token)
    }
    pub fn authorize(&self, token: &str) -> Result<()> {
        if token.len() != 43 {
            return Err(Error::Unauthorized);
        }
        let _lock = self.lock()?;
        let store = self.load()?;
        let hash = digest(token);
        let authorized = store
            .tokens
            .iter()
            .fold(false, |found, record| equal(&hash, &record.hash) | found);
        if authorized {
            Ok(())
        } else {
            Err(Error::Unauthorized)
        }
    }
    pub fn revoke(&mut self, token: &str) -> Result<bool> {
        let _lock = self.lock()?;
        let mut store = self.load()?;
        let before = store.tokens.len();
        let hash = digest(token);
        store.tokens.retain(|record| !equal(&hash, &record.hash));
        let removed = before != store.tokens.len();
        if removed {
            self.save(&store)?;
        }
        Ok(removed)
    }
}

/// Only a local operator may construct a window. Never expose `begin` on a LAN route.
/// One installation-wide window limits brute-force attempts even across source IPs.
pub struct PairingWindow {
    hash: String,
    began: u64,
    expires: u64,
    remaining: u32,
    used: bool,
}
impl PairingWindow {
    pub fn begin(now: u64, duration_secs: u64, attempts: u32) -> Result<(Self, String)> {
        if duration_secs == 0 || duration_secs > 300 || attempts == 0 || attempts > 10 {
            return Err(Error::Unsafe(
                "pairing allows 1–300 seconds and 1–10 attempts".into(),
            ));
        }
        let code = random_secret();
        let expires = now
            .checked_add(duration_secs)
            .ok_or_else(|| Error::Unsafe("pairing clock overflow".into()))?;
        Ok((
            Self {
                hash: digest(&code),
                began: now,
                expires,
                remaining: attempts,
                used: false,
            },
            code,
        ))
    }
    pub fn redeem(&mut self, code: &str, now: u64) -> Result<()> {
        if self.used || now < self.began || now >= self.expires || self.remaining == 0 {
            return Err(Error::PairingUnavailable);
        }
        self.remaining -= 1;
        if code.len() != 43 || !equal(&self.hash, &digest(code)) {
            return Err(Error::Unauthorized);
        }
        self.used = true;
        Ok(())
    }
}

pub struct AccessPolicy {
    hosts: BTreeSet<String>,
    origins: BTreeSet<String>,
    require_tls: bool,
}
fn host(value: &str) -> Result<String> {
    if value.is_empty()
        || value.len() > 255
        || value.bytes().any(|b| {
            b.is_ascii_whitespace() || matches!(b, b'/' | b'@' | b'?' | b'#' | b'%' | b'\\')
        })
    {
        return Err(Error::Unauthorized);
    }
    let url = Url::parse(&format!("https://{value}/")).map_err(|_| Error::Unauthorized)?;
    if url.host_str().is_none() || !url.username().is_empty() || url.password().is_some() {
        return Err(Error::Unauthorized);
    }
    Ok(value.to_ascii_lowercase())
}
fn origin(value: &str) -> Result<String> {
    let url = Url::parse(value).map_err(|_| Error::Unauthorized)?;
    if url.scheme() != "https"
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || url.path() != "/"
        || url.host_str().is_none()
    {
        return Err(Error::Unauthorized);
    }
    let canonical = url.origin().ascii_serialization();
    if value != canonical {
        return Err(Error::Unauthorized);
    }
    Ok(canonical)
}
impl AccessPolicy {
    pub fn lan(hosts: &[&str], origins: &[&str]) -> Result<Self> {
        if hosts.is_empty() || origins.is_empty() {
            return Err(Error::Unsafe(
                "LAN hosts and origins must be explicit".into(),
            ));
        }
        Ok(Self {
            hosts: hosts.iter().map(|s| host(s)).collect::<Result<_>>()?,
            origins: origins.iter().map(|s| origin(s)).collect::<Result<_>>()?,
            require_tls: true,
        })
    }
    pub fn check(&self, request_host: &str, request_origin: Option<&str>, tls: bool) -> Result<()> {
        if self.require_tls && !tls {
            return Err(Error::Unauthorized);
        }
        if !self.hosts.contains(&host(request_host)?) {
            return Err(Error::Unauthorized);
        }
        if let Some(value) = request_origin
            && !self.origins.contains(&origin(value)?)
        {
            return Err(Error::Unauthorized);
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum SecurityMode {
    LegacyLoopback,
    LanTls,
}
impl SecurityMode {
    /// Existing installations remain explicitly loopback until the operator opts into LAN migration.
    pub fn for_installation(existing: bool, opt_in_lan: bool) -> Self {
        if existing && !opt_in_lan {
            Self::LegacyLoopback
        } else {
            Self::LanTls
        }
    }
    pub fn validate_bind(self, bind: std::net::SocketAddr) -> Result<()> {
        if self == Self::LegacyLoopback && !bind.ip().is_loopback() {
            return Err(Error::Unsafe(
                "legacy compatibility is loopback-only; LAN requires TLS and pairing".into(),
            ));
        }
        Ok(())
    }
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct IdentityBundle {
    version: u32,
    certificate_pem: String,
    private_key_pem: String,
    trust_anchor_pem: String,
}
/// Private key never implements Debug and is persisted only inside owner-only state.
pub struct TlsIdentity {
    bundle: IdentityBundle,
}
impl TlsIdentity {
    pub fn create(directory: &Path, names: Vec<String>) -> Result<Self> {
        use rcgen::{
            BasicConstraints, CertificateParams, DnType, ExtendedKeyUsagePurpose, IsCa, Issuer,
            KeyPair, KeyUsagePurpose,
        };
        use time::{Duration, OffsetDateTime};
        private_directory(directory)?;
        let _lock = Lease::acquire(&directory.join("identity.lock"))?;
        if directory.join("identity.json").exists() {
            return Err(Error::Unsafe(
                "identity already exists; explicit re-enrollment is required for rotation".into(),
            ));
        }
        if names.is_empty()
            || names.len() > 16
            || names
                .iter()
                .any(|n| n.is_empty() || n.len() > 253 || n.contains('*'))
        {
            return Err(Error::Unsafe(
                "explicit server DNS names/IPs are required".into(),
            ));
        }
        let failure = |_| Error::Unsafe("certificate generation failed".into());
        let now = OffsetDateTime::now_utc();
        let mut ca = CertificateParams::new(Vec::<String>::new()).map_err(failure)?;
        ca.is_ca = IsCa::Ca(BasicConstraints::Constrained(0));
        ca.distinguished_name.push(
            DnType::CommonName,
            "OpenRadioInspector local installation CA",
        );
        ca.key_usages = vec![
            KeyUsagePurpose::KeyCertSign,
            KeyUsagePurpose::DigitalSignature,
        ];
        ca.not_before = now - Duration::minutes(5);
        ca.not_after = now + Duration::days(730);
        let ca_key = KeyPair::generate().map_err(failure)?;
        let ca_cert = ca.self_signed(&ca_key).map_err(failure)?;
        let issuer = Issuer::new(ca, ca_key);
        let mut leaf = CertificateParams::new(names).map_err(failure)?;
        leaf.not_before = now - Duration::minutes(5);
        leaf.not_after = now + Duration::days(365);
        leaf.key_usages = vec![KeyUsagePurpose::DigitalSignature];
        leaf.extended_key_usages = vec![ExtendedKeyUsagePurpose::ServerAuth];
        leaf.use_authority_key_identifier_extension = true;
        let key = KeyPair::generate().map_err(failure)?;
        let cert = leaf.signed_by(&key, &issuer).map_err(failure)?;
        // Do not retain the CA signing key: this installation CA cannot mint additional identities later.
        let bundle = IdentityBundle {
            version: 1,
            certificate_pem: cert.pem(),
            private_key_pem: key.serialize_pem(),
            trust_anchor_pem: ca_cert.pem(),
        };
        atomic_private(
            &directory.join("identity.json"),
            &serde_json::to_vec(&bundle)?,
        )?;
        Ok(Self { bundle })
    }
    pub fn load(directory: &Path) -> Result<Self> {
        private_directory(directory)?;
        let bundle: IdentityBundle =
            serde_json::from_slice(&read_private(&directory.join("identity.json"), 65536)?)?;
        if bundle.version != 1 {
            return Err(Error::Unsafe("unsupported TLS state".into()));
        }
        let identity = Self { bundle };
        identity.server_config()?;
        Ok(identity)
    }
    pub fn fingerprint_sha256(&self) -> String {
        let der = rustls_pemfile::certs(&mut self.bundle.certificate_pem.as_bytes())
            .next()
            .and_then(|c| c.ok());
        match der {
            Some(der) => Sha256::digest(der.as_ref())
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect(),
            None => String::new(),
        }
    }
    pub fn trust_anchor_pem(&self) -> &str {
        &self.bundle.trust_anchor_pem
    }
    pub fn certificate_pem(&self) -> &str {
        &self.bundle.certificate_pem
    }
    pub fn server_config(&self) -> Result<rustls::ServerConfig> {
        let mut certificates = rustls_pemfile::certs(&mut self.bundle.certificate_pem.as_bytes())
            .collect::<std::result::Result<Vec<_>, _>>()?;
        certificates.extend(
            rustls_pemfile::certs(&mut self.bundle.trust_anchor_pem.as_bytes())
                .collect::<std::result::Result<Vec<_>, _>>()?,
        );
        let key = rustls_pemfile::private_key(&mut self.bundle.private_key_pem.as_bytes())?
            .ok_or_else(|| Error::Unsafe("missing TLS private key".into()))?;
        rustls::ServerConfig::builder_with_provider(std::sync::Arc::new(
            rustls::crypto::ring::default_provider(),
        ))
        .with_safe_default_protocol_versions()
        .map_err(|_| Error::Unsafe("TLS versions unavailable".into()))?
        .with_no_client_auth()
        .with_single_cert(certificates, key)
        .map_err(|_| Error::Unsafe("TLS certificate/key mismatch".into()))
    }
}
