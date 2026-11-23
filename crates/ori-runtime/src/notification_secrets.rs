//! Owner-only outbound destinations. The public archive keeps an opaque revision
//! so an older queued alert cannot be redirected by changing the destination.
use crate::{
    Error, Result,
    lease::{Lease, atomic_private, read_private, state_directory},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
use url::Url;

pub enum Destination {
    Telegram { bot_token: String, chat_id: String },
    Webhook { url: String, secret: String },
}
#[derive(Default, Deserialize, Serialize)]
struct Store {
    telegram: Channel,
    webhook: Channel,
}
#[derive(Default, Deserialize, Serialize, PartialEq, Clone)]
struct Channel {
    enabled: bool,
    first: String,
    second: String,
    revision: String,
}
#[derive(Deserialize, Default)]
#[serde(deny_unknown_fields)]
struct TelegramPatch {
    enabled: Option<bool>,
    bot_token: Option<String>,
    chat_id: Option<String>,
}
#[derive(Deserialize, Default)]
#[serde(deny_unknown_fields)]
struct WebhookPatch {
    enabled: Option<bool>,
    url: Option<String>,
    secret: Option<String>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Patch {
    telegram: Option<TelegramPatch>,
    webhook: Option<WebhookPatch>,
}
fn invalid() -> Error {
    Error::Unsafe("invalid notification channel settings".into())
}
pub fn webhook_url(raw: &str) -> Result<Url> {
    let url = Url::parse(raw).map_err(|_| invalid())?;
    if raw.len() > 2048
        || !matches!(url.scheme(), "https" | "http")
        || url.host().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.fragment().is_some()
    {
        return Err(invalid());
    }
    Ok(url)
}
fn paths(data: &Path, project: &str) -> Result<(PathBuf, PathBuf)> {
    let dir = state_directory(data)?.join("notifications");
    #[cfg(windows)]
    crate::windows_node::ensure_archive_directory(&dir)?;
    #[cfg(not(windows))]
    crate::lease::private_directory(&dir)?;
    let id: String = Sha256::digest(project.as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    Ok((
        dir.join(format!("{id}.json")),
        dir.join(format!("{id}.lock")),
    ))
}
fn load(path: &Path) -> Result<Store> {
    match read_private(path, 16 * 1024) {
        Ok(bytes) => serde_json::from_slice(&bytes).map_err(|_| invalid()),
        Err(Error::Io(e)) if e.kind() == std::io::ErrorKind::NotFound => Ok(Store::default()),
        Err(e) => Err(e),
    }
}
impl Channel {
    fn update(
        &mut self,
        enabled: Option<bool>,
        first: Option<String>,
        second: Option<String>,
    ) -> Result<()> {
        let original = self.clone();
        if let Some(v) = enabled {
            self.enabled = v;
        }
        if let Some(v) = first {
            self.first = v;
        }
        if let Some(v) = second {
            self.second = v;
        }
        if self.enabled && !self.configured() {
            return Err(invalid());
        }
        if self != &original {
            self.revision = crate::security::random_secret();
        }
        Ok(())
    }
    fn configured(&self) -> bool {
        !self.first.is_empty() && !self.second.is_empty()
    }
    fn status(&self) -> Value {
        json!({"enabled":self.enabled,"configured":self.configured(),"revision":self.revision})
    }
}
pub fn configure(data: &Path, project: &str, patch: Value) -> Result<Value> {
    let patch: Patch = serde_json::from_value(patch).map_err(|_| invalid())?;
    if patch.telegram.is_none() && patch.webhook.is_none() {
        return Err(invalid());
    }
    let (path, lock) = paths(data, project)?;
    let _held = Lease::acquire(&lock)?;
    let mut stored = load(&path)?;
    if let Some(p) = patch.telegram {
        if p.bot_token.as_ref().is_some_and(|s| {
            !s.is_empty()
                && (s.len() > 256
                    || !s.split_once(':').is_some_and(|(bot, token)| {
                        !bot.is_empty()
                            && bot.bytes().all(|b| b.is_ascii_digit())
                            && !token.is_empty()
                            && token
                                .bytes()
                                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_'))
                    }))
        }) {
            return Err(invalid());
        }
        if p.chat_id.as_ref().is_some_and(|s| {
            !s.is_empty()
                && (s.len() > 64
                    || !(s.parse::<i64>().is_ok()
                        || s.strip_prefix('@').is_some_and(|v| {
                            !v.is_empty()
                                && v.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
                        })))
        }) {
            return Err(invalid());
        }
        stored.telegram.update(p.enabled, p.bot_token, p.chat_id)?;
    }
    if let Some(p) = patch.webhook {
        if let Some(url) = p.url.as_deref().filter(|v| !v.is_empty()) {
            webhook_url(url)?;
        }
        if p.secret
            .as_ref()
            .is_some_and(|v| v.len() > 1024 || v.chars().any(char::is_control))
        {
            return Err(invalid());
        }
        stored.webhook.update(p.enabled, p.url, p.secret)?;
    }
    atomic_private(&path, &serde_json::to_vec(&stored)?)?;
    Ok(json!({"telegram":stored.telegram.status(),"webhook":stored.webhook.status()}))
}
pub fn destination(
    data: &Path,
    project: &str,
    name: &str,
    revision: &str,
) -> Result<Option<Destination>> {
    let (path, _lock) = paths(data, project)?;
    let stored = load(&path)?;
    let channel = match name {
        "telegram" => stored.telegram,
        "webhook" => stored.webhook,
        _ => return Err(invalid()),
    };
    if !channel.enabled
        || !channel.configured()
        || revision.is_empty()
        || channel.revision != revision
    {
        return Ok(None);
    }
    Ok(Some(if name == "telegram" {
        Destination::Telegram {
            bot_token: channel.first,
            chat_id: channel.second,
        }
    } else {
        Destination::Webhook {
            url: channel.first,
            secret: channel.second,
        }
    }))
}
