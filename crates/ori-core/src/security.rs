//! Transport-bound LAN authentication. Header assertions cannot manufacture TLS.
mod receipt;
use crate::{
    CoreError, LanSecurityConfig, Result,
    error::{ApiError, ApiResult},
};
use axum::{
    http::{HeaderMap, Method, StatusCode, header},
    response::Response,
};
use ori_runtime::security::{AccessPolicy, Credentials, PairingWindow, TlsIdentity};
pub(crate) use receipt::AuthReceipt;
use receipt::Revocation;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex, Weak},
    time::{SystemTime, UNIX_EPOCH},
};
use tokio::sync::watch;

#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum ListenerRole {
    Plain,
    Tls,
    Control,
}
#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum AuthKind {
    Legacy,
    Bearer,
    Cookie,
    Unauthenticated,
}
#[derive(Clone)]
pub(crate) struct AuthContext {
    pub kind: AuthKind,
    token: Option<String>,
    receipt: AuthReceipt,
}
impl AuthContext {
    pub(crate) fn receipt(&self) -> AuthReceipt {
        self.receipt.clone()
    }
}
struct Private {
    credentials: Credentials,
    pairing: Option<PairingWindow>,
    receipts: BTreeMap<[u8; 32], Weak<Revocation>>,
}
pub(crate) struct SecurityState {
    policy: Option<AccessPolicy>,
    private: Option<Mutex<Private>>,
    tls: Option<axum_server::tls_rustls::RustlsConfig>,
    pub revocations: watch::Sender<u64>,
    fingerprint: Option<String>,
    admission: Arc<Revocation>,
}
fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |duration| duration.as_secs())
}
fn denial() -> ApiError {
    ApiError::new(
        StatusCode::UNAUTHORIZED,
        "unauthorized",
        "Требуется действующая учётная запись клиента",
    )
}
fn forbidden() -> ApiError {
    ApiError::new(
        StatusCode::FORBIDDEN,
        "untrusted_request_origin",
        "Неверный Host, Origin или TLS transport",
    )
}
fn runtime_error(error: ori_runtime::Error) -> ApiError {
    match error {
        ori_runtime::Error::Unauthorized => denial(),
        ori_runtime::Error::PairingUnavailable => ApiError::new(
            StatusCode::TOO_MANY_REQUESTS,
            "pairing_unavailable",
            "Локальное окно сопряжения закрыто или исчерпано",
        ),
        _ => ApiError::unavailable("security_state_unavailable"),
    }
}
impl SecurityState {
    pub fn open(config: Option<&LanSecurityConfig>) -> Result<Arc<Self>> {
        let (revocations, _) = watch::channel(0);
        let admission = Revocation::live();
        let Some(config) = config else {
            return Ok(Arc::new(Self {
                policy: None,
                private: None,
                tls: None,
                revocations,
                fingerprint: None,
                admission,
            }));
        };
        let hosts = config.hosts.iter().map(String::as_str).collect::<Vec<_>>();
        let origins = config
            .origins
            .iter()
            .map(String::as_str)
            .collect::<Vec<_>>();
        let policy = AccessPolicy::lan(&hosts, &origins)?;
        let identity = TlsIdentity::load(&config.directory)?;
        let tls =
            axum_server::tls_rustls::RustlsConfig::from_config(Arc::new(identity.server_config()?));
        let credentials = Credentials::open(&config.directory)?;
        Ok(Arc::new(Self {
            policy: Some(policy),
            private: Some(Mutex::new(Private {
                credentials,
                pairing: None,
                receipts: BTreeMap::new(),
            })),
            tls: Some(tls),
            revocations,
            fingerprint: Some(identity.fingerprint_sha256()),
            admission,
        }))
    }
    pub fn lan(&self) -> bool {
        self.policy.is_some()
    }
    pub fn tls_config(&self) -> Result<axum_server::tls_rustls::RustlsConfig> {
        self.tls.clone().ok_or(CoreError::Invalid(
            "TLS requires an explicitly provisioned private LAN identity",
        ))
    }
    pub fn fingerprint(&self) -> Option<&str> {
        self.fingerprint.as_deref()
    }
    pub(crate) fn begin_shutdown(&self) {
        self.admission.revoke();
    }
    async fn authenticate(
        self: &Arc<Self>,
        kind: AuthKind,
        token: String,
    ) -> ApiResult<AuthContext> {
        let state = self.clone();
        tokio::task::spawn_blocking(move || {
            let mut private = state
                .private
                .as_ref()
                .ok_or_else(denial)?
                .lock()
                .map_err(|_| ApiError::unavailable("security_state_unavailable"))?;
            let key: [u8; 32] = Sha256::digest(token.as_bytes()).into();
            if let Err(error) = private.credentials.authorize(&token) {
                if let Some(flag) = private.receipts.get(&key).and_then(Weak::upgrade) {
                    flag.revoke();
                }
                return Err(runtime_error(error));
            }
            private.receipts.retain(|_, flag| flag.strong_count() > 0);
            let flag = if let Some(flag) = private
                .receipts
                .get(&key)
                .and_then(Weak::upgrade)
                .filter(|flag| flag.active())
            {
                flag
            } else {
                if private.receipts.len() >= 1024 {
                    return Err(ApiError::unavailable("auth_receipt_capacity"));
                }
                let flag = Revocation::live();
                private.receipts.insert(key, Arc::downgrade(&flag));
                flag
            };
            let receipt = AuthReceipt::new(state.admission.clone(), Some(flag));
            receipt.revalidate()?;
            Ok(AuthContext {
                kind,
                token: Some(token),
                receipt,
            })
        })
        .await
        .map_err(|_| ApiError::unavailable("security_state_unavailable"))?
    }
    pub fn begin(&self, ttl: u64, attempts: u32) -> Result<String> {
        let private = self.private.as_ref().ok_or(CoreError::Invalid(
            "explicit legacy loopback mode requires no pairing",
        ))?;
        let (window, code) = PairingWindow::begin(now(), ttl, attempts)?;
        private
            .lock()
            .map_err(|_| CoreError::Invalid("security state poisoned"))?
            .pairing = Some(window);
        Ok(code)
    }
    pub async fn check(
        self: &Arc<Self>,
        method: &Method,
        path: &str,
        headers: &HeaderMap,
        role: ListenerRole,
    ) -> ApiResult<AuthContext> {
        if !self.admission.active() {
            return Err(ApiError::unavailable("runtime_quiescing"));
        }
        if let Some(policy) = &self.policy {
            let hosts = headers.get_all(header::HOST).iter().collect::<Vec<_>>();
            let origins = headers.get_all(header::ORIGIN).iter().collect::<Vec<_>>();
            if hosts.len() != 1 || origins.len() > 1 {
                return Err(forbidden());
            }
            let host = hosts[0].to_str().map_err(|_| forbidden())?;
            let origin = origins
                .first()
                .map(|value| value.to_str().map_err(|_| forbidden()))
                .transpose()?;
            policy
                .check(host, origin, role == ListenerRole::Tls)
                .map_err(|_| forbidden())?;
            let optional = path == "/api/v1/security/status"
                || path == "/api/v1/security/pair"
                || ((method == Method::GET || method == Method::HEAD)
                    && crate::assets::contains(path));
            let context = match credential(headers)? {
                Some((kind, token)) => match self.authenticate(kind, token).await {
                    Ok(context) => context,
                    Err(_)
                        if path == "/api/v1/security/status" || path == "/api/v1/security/pair" =>
                    {
                        AuthContext {
                            kind: AuthKind::Unauthenticated,
                            token: None,
                            receipt: AuthReceipt::new(self.admission.clone(), None),
                        }
                    }
                    Err(error) => return Err(error),
                },
                None if optional => AuthContext {
                    kind: AuthKind::Unauthenticated,
                    token: None,
                    receipt: AuthReceipt::new(self.admission.clone(), None),
                },
                None => return Err(denial()),
            };
            if context.kind == AuthKind::Cookie
                && method != Method::GET
                && method != Method::HEAD
                && origin.is_none()
            {
                return Err(forbidden());
            }
            Ok(context)
        } else {
            // Preserve explicit legacy loopback compatibility, including its mutation-origin check.
            if method != Method::GET && method != Method::HEAD {
                let origins = headers.get_all(header::ORIGIN).iter().collect::<Vec<_>>();
                if origins.len() > 1 {
                    return Err(forbidden());
                }
                if let Some(origin) = origins.first() {
                    let host = headers
                        .get(header::HOST)
                        .and_then(|v| v.to_str().ok())
                        .ok_or_else(forbidden)?;
                    let origin = origin.to_str().map_err(|_| forbidden())?;
                    if origin != format!("http://{host}") && origin != format!("https://{host}") {
                        return Err(forbidden());
                    }
                }
            }
            Ok(AuthContext {
                kind: AuthKind::Legacy,
                token: None,
                receipt: AuthReceipt::new(self.admission.clone(), None),
            })
        }
    }
    pub async fn validate(self: &Arc<Self>, context: &AuthContext) -> ApiResult<()> {
        context.receipt.revalidate()?;
        if context.kind == AuthKind::Legacy {
            return Ok(());
        }
        let token = context.token.clone().ok_or_else(denial)?;
        let state = self.clone();
        let result = tokio::task::spawn_blocking(move || {
            let private = state.private.as_ref().ok_or_else(denial)?;
            private
                .lock()
                .map_err(|_| ApiError::unavailable("security_state_unavailable"))?
                .credentials
                .authorize(&token)
                .map_err(runtime_error)
        })
        .await
        .unwrap_or_else(|_| Err(ApiError::unavailable("security_state_unavailable")));
        if result.is_err() {
            context.receipt.revoke_credential();
        }
        result
    }
    pub fn status(&self, context: &AuthContext) -> Value {
        json!({"mode":if self.lan(){"lan_tls"}else{"legacy_loopback"},"pairing_required":self.lan()&&context.kind==AuthKind::Unauthenticated})
    }
    pub async fn pair(
        self: &Arc<Self>,
        payload: Value,
        headers: &HeaderMap,
    ) -> ApiResult<Response> {
        if !self.lan() {
            return Err(ApiError::new(
                StatusCode::CONFLICT,
                "pairing_not_required",
                "Legacy loopback mode requires no pairing",
            ));
        }
        let allowed = ["pairing_code", "label", "client"];
        if payload
            .as_object()
            .is_none_or(|v| v.keys().any(|key| !allowed.contains(&key.as_str())))
        {
            return Err(ApiError::invalid("Некорректный запрос сопряжения"));
        }
        let code = payload["pairing_code"]
            .as_str()
            .ok_or_else(|| ApiError::invalid("Требуется код сопряжения"))?
            .to_owned();
        let label = payload["label"]
            .as_str()
            .ok_or_else(|| ApiError::invalid("Требуется имя клиента"))?
            .to_owned();
        if label.is_empty() || label.len() > 128 || label.chars().any(char::is_control) {
            return Err(ApiError::invalid("Некорректное имя клиента"));
        }
        let native = match payload.get("client") {
            None => false,
            Some(Value::String(client)) if client == "browser" => false,
            Some(Value::String(client)) if client == "native" => true,
            _ => return Err(ApiError::invalid("Неизвестный тип клиента")),
        };
        if !native && !headers.contains_key(header::ORIGIN) {
            return Err(forbidden());
        }
        let state = self.clone();
        let token = tokio::task::spawn_blocking(move || {
            let mut private = state
                .private
                .as_ref()
                .ok_or_else(denial)?
                .lock()
                .map_err(|_| ApiError::unavailable("security_state_unavailable"))?;
            private
                .pairing
                .as_mut()
                .ok_or_else(|| runtime_error(ori_runtime::Error::PairingUnavailable))?
                .redeem(&code, now())
                .map_err(runtime_error)?;
            private
                .credentials
                .issue(&label, now())
                .map_err(runtime_error)
        })
        .await
        .map_err(|_| ApiError::unavailable("security_state_unavailable"))??;
        let data = if native {
            json!({"paired":true,"credential":"bearer","token":token})
        } else {
            json!({"paired":true,"credential":"browser_cookie"})
        };
        let mut response = super::http::envelope(StatusCode::OK, data);
        set_cookie(&mut response, &token, false)?;
        Ok(response)
    }
    pub fn browser_session(&self, context: &AuthContext, payload: &Value) -> ApiResult<Response> {
        context.receipt.revalidate()?;
        if context.kind != AuthKind::Bearer {
            return Err(denial());
        }
        empty(payload)?;
        let mut response = super::http::envelope(
            StatusCode::OK,
            json!({"paired":true,"credential":"browser_cookie"}),
        );
        set_cookie(
            &mut response,
            context.token.as_deref().ok_or_else(denial)?,
            false,
        )?;
        Ok(response)
    }
    pub async fn revoke(
        self: &Arc<Self>,
        context: &AuthContext,
        payload: &Value,
    ) -> ApiResult<Response> {
        empty(payload)?;
        let token = context.token.clone().ok_or_else(denial)?;
        let state = self.clone();
        let receipt = context.receipt();
        let revoked = tokio::task::spawn_blocking(move || {
            let mut private = state
                .private
                .as_ref()
                .ok_or_else(denial)?
                .lock()
                .map_err(|_| ApiError::unavailable("security_state_unavailable"))?;
            receipt.revalidate()?;
            let revoked = private.credentials.revoke(&token).map_err(runtime_error)?;
            let key: [u8; 32] = Sha256::digest(token.as_bytes()).into();
            if let Some(flag) = private.receipts.get(&key).and_then(Weak::upgrade) {
                flag.revoke();
            }
            Ok::<_, ApiError>(revoked)
        })
        .await
        .map_err(|_| ApiError::unavailable("security_state_unavailable"))??;
        self.revocations
            .send_modify(|generation| *generation = generation.wrapping_add(1));
        let mut response = super::http::envelope(StatusCode::OK, json!({"revoked":revoked}));
        set_cookie(&mut response, "", true)?;
        Ok(response)
    }
}
fn empty(value: &Value) -> ApiResult<()> {
    if value.as_object().is_none_or(|map| !map.is_empty()) {
        return Err(ApiError::invalid("Ожидается пустой объект"));
    }
    Ok(())
}
fn set_cookie(response: &mut Response, token: &str, clear: bool) -> ApiResult<()> {
    let value = if clear {
        "ori_session=; Secure; HttpOnly; SameSite=Strict; Path=/api; Max-Age=0".to_owned()
    } else {
        format!("ori_session={token}; Secure; HttpOnly; SameSite=Strict; Path=/api")
    };
    response.headers_mut().insert(
        header::SET_COOKIE,
        value
            .parse()
            .map_err(|_| ApiError::unavailable("security_state_unavailable"))?,
    );
    Ok(())
}
fn credential(headers: &HeaderMap) -> ApiResult<Option<(AuthKind, String)>> {
    let auth = headers
        .get_all(header::AUTHORIZATION)
        .iter()
        .collect::<Vec<_>>();
    if auth.len() > 1 {
        return Err(denial());
    }
    let mut cookie = None;
    for header in headers.get_all(header::COOKIE).iter() {
        let value = header.to_str().map_err(|_| denial())?;
        if value.len() > 8192 {
            return Err(denial());
        }
        for item in value.split(';') {
            if let Some((name, value)) = item.trim().split_once('=')
                && name == "ori_session"
                && cookie.replace(value.to_owned()).is_some()
            {
                return Err(denial());
            }
        }
    }
    if !auth.is_empty() && cookie.is_some() {
        return Err(denial());
    }
    if let Some(auth) = auth.first() {
        let value = auth.to_str().map_err(|_| denial())?;
        let token = value
            .strip_prefix("Bearer ")
            .filter(|s| {
                s.len() == 43
                    && s.bytes()
                        .all(|c| c.is_ascii_alphanumeric() || b"_-".contains(&c))
            })
            .ok_or_else(denial)?;
        return Ok(Some((AuthKind::Bearer, token.into())));
    }
    Ok(cookie.map(|token| (AuthKind::Cookie, token)))
}

#[cfg(test)]
mod receipt_tests;
