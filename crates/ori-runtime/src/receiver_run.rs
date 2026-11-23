//! Node-private AR-DV1 run bindings. Only a random run ID reaches the measurement archive.
use crate::{
    Error, Result,
    lease::{Lease, atomic_private, read_private, state_directory},
};
use rand::RngCore;
use ring::hmac;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    path::{Path, PathBuf},
    sync::Mutex,
};

fn unavailable() -> Error {
    Error::Unsafe(
        "private AR-DV1 run binding unavailable or incompatible; start a new session".into(),
    )
}

fn random() -> Result<[u8; 32]> {
    let mut bytes = [0; 32];
    rand::rngs::OsRng
        .try_fill_bytes(&mut bytes)
        .map_err(|_| unavailable())?;
    Ok(bytes)
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Binding {
    version: u8,
    public_id: String,
    key: [u8; 32],
    verifier: [u8; 32],
}

pub struct ReceiverRun {
    path: PathBuf,
    scope: [u8; 32],
    expected: Mutex<Option<String>>,
}

impl ReceiverRun {
    /// Before opening the port, require the local association for every archived original.
    pub fn open(
        node: &Path,
        session: &str,
        stream: &str,
        archived: Option<String>,
        resume_empty: bool,
    ) -> Result<Self> {
        let dir = state_directory(node)?.join("receiver-runs");
        #[cfg(windows)]
        crate::windows_node::ensure_archive_directory(&dir)?;
        #[cfg(not(windows))]
        crate::lease::private_directory(&dir)?;
        let mut scope = Sha256::new();
        for part in [session.as_bytes(), stream.as_bytes()] {
            scope.update((part.len() as u64).to_be_bytes());
            scope.update(part);
        }
        let scope: [u8; 32] = scope.finalize().into();
        let run = Self {
            path: dir.join(format!("{}.json", hex(&scope))),
            scope,
            expected: Mutex::new(archived),
        };
        {
            let mut expected = run.expected.lock().map_err(|_| unavailable())?;
            let stored = run.load(expected.as_deref())?;
            if resume_empty && stored.is_none() {
                return Err(unavailable());
            }
            if let Some(binding) = stored {
                *expected = Some(binding.public_id);
            }
        }
        Ok(run)
    }

    fn load(&self, expected: Option<&str>) -> Result<Option<Binding>> {
        let raw = match read_private(&self.path, 2048) {
            Ok(value) => value,
            Err(Error::Io(e)) if e.kind() == std::io::ErrorKind::NotFound && expected.is_none() => {
                return Ok(None);
            }
            Err(_) => return Err(unavailable()),
        };
        let value: Binding = serde_json::from_slice(&raw).map_err(|_| unavailable())?;
        if value.version != 1
            || value.public_id.len() != 64
            || !value
                .public_id
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            || expected.is_some_and(|id| id != value.public_id)
        {
            return Err(unavailable());
        }
        Ok(Some(value))
    }

    /// Verify RN in owner-private state before the adapter can tune or publish any RX record.
    pub fn confirm(&self, serial: &str) -> Result<String> {
        if !serial.starts_with("RN") || !(3..=1024).contains(&serial.len()) {
            return Err(unavailable());
        }
        let mut expected = self.expected.lock().map_err(|_| unavailable())?;
        let _lock = Lease::acquire(&self.path.with_extension("lock"))?;
        let existing = self.load(expected.as_deref())?;
        let saved = existing.is_some();
        let mut binding = match existing {
            Some(value) => value,
            None => Binding {
                version: 1,
                public_id: hex(&random()?),
                key: random()?,
                verifier: [0; 32],
            },
        };
        let key = hmac::Key::new(hmac::HMAC_SHA256, &binding.key);
        let mut message = Vec::with_capacity(96 + serial.len());
        message.extend_from_slice(&self.scope);
        message.extend_from_slice(binding.public_id.as_bytes());
        message.extend_from_slice(serial.as_bytes());
        if saved {
            hmac::verify(&key, &message, &binding.verifier).map_err(|_| unavailable())?;
        } else {
            binding
                .verifier
                .copy_from_slice(hmac::sign(&key, &message).as_ref());
            atomic_private(&self.path, &serde_json::to_vec(&binding)?)?;
        }
        *expected = Some(binding.public_id.clone());
        Ok(binding.public_id)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn same_synthetic_identity_is_independent_per_node_and_run_and_survives_restart() {
        let left = tempfile::tempdir().unwrap();
        let right = tempfile::tempdir().unwrap();
        let serial = "RN-test-fixture";
        assert!(ReceiverRun::open(left.path(), "session", "stream", None, true).is_err());
        let run = ReceiverRun::open(left.path(), "session", "stream", None, false).unwrap();
        let id = run.confirm(serial).unwrap();
        let pending = ReceiverRun::open(left.path(), "session", "stream", None, true).unwrap();
        assert_eq!(id, pending.confirm(serial).unwrap());
        let other_node = ReceiverRun::open(right.path(), "session", "stream", None, false).unwrap();
        let other_session =
            ReceiverRun::open(left.path(), "another", "stream", None, false).unwrap();
        assert_ne!(id, other_node.confirm(serial).unwrap());
        assert_ne!(id, other_session.confirm(serial).unwrap());
        let persisted = fs::read_to_string(&run.path).unwrap();
        assert!(!persisted.contains(serial));
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                fs::metadata(&run.path).unwrap().permissions().mode() & 0o077,
                0
            );
        }
        let reopened =
            ReceiverRun::open(left.path(), "session", "stream", Some(id.clone()), false).unwrap();
        assert_eq!(id, reopened.confirm(serial).unwrap());
        assert!(reopened.confirm("RN-other-fixture").is_err());
        fs::remove_file(&run.path).unwrap();
        assert!(reopened.confirm(serial).is_err());
        assert!(ReceiverRun::open(left.path(), "session", "stream", Some(id), false).is_err());
        assert!(!run.path.exists());
    }

    #[test]
    fn swapped_or_corrupt_bindings_never_replace_the_recorded_identity() {
        let node = tempfile::tempdir().unwrap();
        let a = ReceiverRun::open(node.path(), "a", "bc", None, false).unwrap();
        let b = ReceiverRun::open(node.path(), "ab", "c", None, false).unwrap();
        let first = a.confirm("RN-synthetic").unwrap();
        let second = b.confirm("RN-synthetic").unwrap();
        assert_ne!(first, second);
        let bytes = fs::read(&a.path).unwrap();
        atomic_private(&b.path, &bytes).unwrap();
        assert!(ReceiverRun::open(node.path(), "ab", "c", Some(second), false).is_err());
        let copied = ReceiverRun::open(node.path(), "ab", "c", Some(first.clone()), false).unwrap();
        assert!(copied.confirm("RN-synthetic").is_err());
        let mut damaged: Binding = serde_json::from_slice(&bytes).unwrap();
        damaged.verifier[0] ^= 1;
        atomic_private(&a.path, &serde_json::to_vec(&damaged).unwrap()).unwrap();
        let corrupted = ReceiverRun::open(node.path(), "a", "bc", Some(first), false).unwrap();
        assert!(corrupted.confirm("RN-synthetic").is_err());
        assert!(corrupted.confirm("RN-another").is_err());
    }
}
