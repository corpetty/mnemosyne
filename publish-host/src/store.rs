//! Publications on disk: `<root>/<id>/{meta.json, manifest, blobs/<sha256>}`.
//!
//! The host never holds a key, so everything stored is ciphertext it cannot read. Every file
//! is written to a temporary name and renamed, so a crash leaves the old or the new content.
//! One lock orders all writes (reads share it): this is a small server, and it keeps the
//! byte count behind the quota exact.

use std::collections::HashMap;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use axum::http::StatusCode;
use serde::{Deserialize, Serialize};
use serde_json::json;
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;
use tokio::fs;
use tokio::io::AsyncWriteExt;
use tokio::sync::RwLock;

use crate::clock::Clock;
use crate::config::Config;
use crate::error::ApiError;

pub fn valid_id(s: &str) -> bool {
    s.len() == 16
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

pub fn valid_sha(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

pub fn sha256_hex(data: &[u8]) -> String {
    Sha256::digest(data)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// Constant-time equality of two secrets of any length (compares their hashes).
pub fn secrets_equal(a: &str, b: &str) -> bool {
    let (a, b) = (Sha256::digest(a.as_bytes()), Sha256::digest(b.as_bytes()));
    a.ct_eq(&b).into()
}

/// Who is writing: the hash of their publisher token, and whether they may start a
/// publication here (always, unless the host is closed with an upload token).
pub struct Auth {
    pub token_hash: String,
    pub upload_ok: bool,
}

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct Meta {
    /// sha256 (hex) of the publisher token; empty in a tombstone.
    #[serde(default)]
    pub token_hash: String,
    /// The manifest's version; 0 while only blobs were uploaded.
    #[serde(default)]
    pub version: u64,
    /// Unix seconds; 0 until there is a manifest.
    #[serde(default)]
    pub expires: u64,
    #[serde(default)]
    pub created: u64,
    #[serde(default)]
    pub updated: u64,
    #[serde(default)]
    pub manifest_sha: Option<String>,
    /// The blobs the current manifest references.
    #[serde(default)]
    pub blobs: Vec<String>,
    /// Set when unpublished or expired: the publication answers 410 for a while.
    #[serde(default)]
    pub deleted_at: Option<u64>,
}

#[derive(Debug, Default, PartialEq, Eq)]
pub struct SweepStats {
    pub expired: usize,
    pub tombstones_removed: usize,
    pub pending_removed: usize,
}

pub struct Store {
    cfg: Arc<Config>,
    clock: Clock,
    /// Bytes held across all publications, guarded by the store-wide lock.
    used: RwLock<u64>,
}

static TMP: AtomicU64 = AtomicU64::new(0);

async fn write_atomic(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let n = TMP.fetch_add(1, Ordering::Relaxed);
    let tmp = path.with_file_name(format!(".tmp-{}-{n}", std::process::id()));
    let mut f = fs::File::create(&tmp).await?;
    let written = async {
        f.write_all(bytes).await?;
        f.sync_all().await
    }
    .await;
    drop(f);
    let done = match written {
        Ok(()) => fs::rename(&tmp, path).await,
        Err(e) => Err(e),
    };
    if done.is_err() {
        let _ = fs::remove_file(&tmp).await;
    }
    done
}

/// The blobs in a publication's folder with their sizes (temporary files are not blobs).
async fn list_blobs(dir: &Path) -> HashMap<String, u64> {
    let mut found = HashMap::new();
    if let Ok(mut rd) = fs::read_dir(dir.join("blobs")).await {
        while let Ok(Some(e)) = rd.next_entry().await {
            let name = e.file_name().to_string_lossy().into_owned();
            if valid_sha(&name) {
                if let Ok(m) = e.metadata().await {
                    found.insert(name, m.len());
                }
            }
        }
    }
    found
}

async fn held_bytes(dir: &Path) -> u64 {
    let manifest = fs::metadata(dir.join("manifest"))
        .await
        .map_or(0, |m| m.len());
    manifest + list_blobs(dir).await.values().sum::<u64>()
}

async fn ids_in(root: &Path) -> Vec<String> {
    let mut ids = Vec::new();
    if let Ok(mut rd) = fs::read_dir(root).await {
        while let Ok(Some(e)) = rd.next_entry().await {
            let name = e.file_name().to_string_lossy().into_owned();
            if valid_id(&name) {
                ids.push(name);
            }
        }
    }
    ids
}

impl Store {
    /// Opens (creating) the data directory, drops half-written temporary files and counts
    /// the bytes held.
    pub async fn open(cfg: Arc<Config>, clock: Clock) -> io::Result<Store> {
        fs::create_dir_all(&cfg.data_dir).await?;
        let store = Store {
            cfg,
            clock,
            used: RwLock::new(0),
        };
        for id in ids_in(&store.cfg.data_dir).await {
            let dir = store.dir(&id);
            for d in [dir.clone(), dir.join("blobs")] {
                if let Ok(mut rd) = fs::read_dir(&d).await {
                    while let Ok(Some(e)) = rd.next_entry().await {
                        if e.file_name().to_string_lossy().starts_with(".tmp-") {
                            let _ = fs::remove_file(e.path()).await;
                        }
                    }
                }
            }
        }
        *store.used.write().await = store.scan_used().await;
        Ok(store)
    }

    fn dir(&self, id: &str) -> PathBuf {
        self.cfg.data_dir.join(id)
    }

    async fn scan_used(&self) -> u64 {
        let mut total = 0;
        for id in ids_in(&self.cfg.data_dir).await {
            total += held_bytes(&self.dir(&id)).await;
        }
        total
    }

    pub async fn used_bytes(&self) -> u64 {
        *self.used.read().await
    }

    async fn read_meta(&self, id: &str) -> Result<Option<Meta>, ApiError> {
        match fs::read(self.dir(id).join("meta.json")).await {
            Ok(bytes) => serde_json::from_slice(&bytes).map(Some).map_err(|e| {
                tracing::error!("meta.json of {id} unreadable: {e}");
                ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, "storage_error")
            }),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e.into()),
        }
    }

    async fn write_meta(&self, id: &str, meta: &Meta) -> Result<(), ApiError> {
        let bytes = serde_json::to_vec(meta).map_err(|e| {
            tracing::error!("meta of {id} not serialisable: {e}");
            ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, "storage_error")
        })?;
        Ok(write_atomic(&self.dir(id).join("meta.json"), &bytes).await?)
    }

    /// The publication as it stands: `None` when there is nothing (or only a tombstone past
    /// its grace), 410 for an unpublished or expired one.
    async fn load(&self, id: &str) -> Result<Option<Meta>, ApiError> {
        let Some(meta) = self.read_meta(id).await? else {
            return Ok(None);
        };
        let gone = || ApiError::new(StatusCode::GONE, "gone");
        let now = self.clock.now();
        if let Some(at) = meta.deleted_at {
            return if now < at + self.cfg.tombstone_days * 86_400 {
                Err(gone())
            } else {
                Ok(None)
            };
        }
        if meta.manifest_sha.is_some() && meta.expires <= now {
            return Err(gone());
        }
        Ok(Some(meta))
    }

    fn authorize(meta: Option<&Meta>, auth: &Auth) -> Result<(), ApiError> {
        let forbidden = |code| ApiError::new(StatusCode::FORBIDDEN, code);
        match meta {
            Some(m) if secrets_equal(&m.token_hash, &auth.token_hash) => Ok(()),
            Some(_) => Err(forbidden("wrong_token")),
            None if auth.upload_ok => Ok(()),
            None => Err(forbidden("upload_token_required")),
        }
    }

    /// Whether this writer may write to `id`, checked before a body is read.
    pub async fn check_writer(&self, id: &str, auth: &Auth) -> Result<(), ApiError> {
        let _guard = self.used.read().await;
        Self::authorize(self.load(id).await?.as_ref(), auth)
    }

    pub async fn get_manifest(&self, id: &str) -> Result<(Meta, Vec<u8>), ApiError> {
        let _guard = self.used.read().await;
        let meta = self.load(id).await?.ok_or_else(not_found)?;
        if meta.manifest_sha.is_none() {
            return Err(not_found());
        }
        let bytes = fs::read(self.dir(id).join("manifest")).await?;
        Ok((meta, bytes))
    }

    pub async fn open_blob(&self, id: &str, sha: &str) -> Result<(fs::File, u64), ApiError> {
        let _guard = self.used.read().await;
        let meta = self.load(id).await?.ok_or_else(not_found)?;
        if !meta.blobs.iter().any(|b| b == sha) {
            return Err(not_found());
        }
        let file = fs::File::open(self.dir(id).join("blobs").join(sha))
            .await
            .map_err(|e| match e.kind() {
                io::ErrorKind::NotFound => not_found(),
                _ => e.into(),
            })?;
        let len = file.metadata().await?.len();
        Ok((file, len))
    }

    /// Stores a blob (its hash was checked against the path by the caller). `true` when it
    /// was new, `false` when the publication already held it.
    pub async fn put_blob(
        &self,
        id: &str,
        sha: &str,
        body: &[u8],
        auth: &Auth,
    ) -> Result<bool, ApiError> {
        let mut used = self.used.write().await;
        let dir = self.dir(id);
        let before = held_bytes(&dir).await;
        let result = self.put_blob_locked(*used, id, sha, body, auth).await;
        *used = used.saturating_sub(before) + held_bytes(&dir).await;
        result
    }

    async fn put_blob_locked(
        &self,
        used: u64,
        id: &str,
        sha: &str,
        body: &[u8],
        auth: &Auth,
    ) -> Result<bool, ApiError> {
        let meta = self.load(id).await?;
        Self::authorize(meta.as_ref(), auth)?;
        let dir = self.dir(id);
        if fs::try_exists(dir.join("blobs").join(sha)).await? {
            return Ok(false);
        }
        let len = body.len() as u64;
        if held_bytes(&dir).await + len > self.cfg.publication_max_bytes {
            return Err(ApiError::new(
                StatusCode::PAYLOAD_TOO_LARGE,
                "publication_too_large",
            ));
        }
        if used + len > self.cfg.quota_bytes {
            return Err(ApiError::new(
                StatusCode::INSUFFICIENT_STORAGE,
                "store_full",
            ));
        }
        let now = self.clock.now();
        match meta {
            // A first write: remember who owns this id before anything else.
            None => {
                fs::create_dir_all(dir.join("blobs")).await?;
                let fresh = Meta {
                    token_hash: auth.token_hash.clone(),
                    created: now,
                    updated: now,
                    ..Meta::default()
                };
                self.write_meta(id, &fresh).await?;
            }
            // Blobs only so far: keep the "never got a manifest" timer fresh.
            Some(mut m) if m.manifest_sha.is_none() => {
                m.updated = now;
                self.write_meta(id, &m).await?;
            }
            Some(_) => {}
        }
        write_atomic(&dir.join("blobs").join(sha), body).await?;
        Ok(true)
    }

    /// Swaps in a new manifest. The caller checked the headers' syntax and the expiry range.
    pub async fn put_manifest(
        &self,
        id: &str,
        manifest: &[u8],
        version: u64,
        expires: u64,
        refs: &[String],
        auth: &Auth,
    ) -> Result<Meta, ApiError> {
        let mut used = self.used.write().await;
        let dir = self.dir(id);
        let before = held_bytes(&dir).await;
        let result = self
            .put_manifest_locked(*used, id, manifest, version, expires, refs, auth)
            .await;
        *used = used.saturating_sub(before) + held_bytes(&dir).await;
        result
    }

    #[allow(clippy::too_many_arguments)]
    async fn put_manifest_locked(
        &self,
        used: u64,
        id: &str,
        manifest: &[u8],
        version: u64,
        expires: u64,
        refs: &[String],
        auth: &Auth,
    ) -> Result<Meta, ApiError> {
        let old = self.load(id).await?;
        Self::authorize(old.as_ref(), auth)?;
        if let Some(m) = &old {
            if version <= m.version {
                return Err(
                    ApiError::new(StatusCode::CONFLICT, "version_not_increasing")
                        .with(json!({ "current": m.version })),
                );
            }
        }
        let dir = self.dir(id);
        let held = list_blobs(&dir).await;
        let missing: Vec<&String> = refs.iter().filter(|r| !held.contains_key(*r)).collect();
        if !missing.is_empty() {
            return Err(ApiError::new(StatusCode::CONFLICT, "missing_blobs")
                .with(json!({ "missing": missing })));
        }
        let new_len = manifest.len() as u64;
        let total = new_len + refs.iter().map(|r| held[r]).sum::<u64>();
        if total > self.cfg.publication_max_bytes {
            return Err(ApiError::new(
                StatusCode::PAYLOAD_TOO_LARGE,
                "publication_too_large",
            ));
        }
        let old_len = fs::metadata(dir.join("manifest"))
            .await
            .map_or(0, |m| m.len());
        if used + new_len.saturating_sub(old_len) > self.cfg.quota_bytes {
            return Err(ApiError::new(
                StatusCode::INSUFFICIENT_STORAGE,
                "store_full",
            ));
        }

        let now = self.clock.now();
        fs::create_dir_all(dir.join("blobs")).await?;
        write_atomic(&dir.join("manifest"), manifest).await?;
        let meta = Meta {
            token_hash: auth.token_hash.clone(),
            version,
            expires,
            created: old.as_ref().map_or(now, |m| m.created),
            updated: now,
            manifest_sha: Some(sha256_hex(manifest)),
            blobs: refs.to_vec(),
            deleted_at: None,
        };
        self.write_meta(id, &meta).await?;
        // The swap is done: blobs the new manifest no longer references can go.
        for sha in held.keys().filter(|s| !refs.contains(s)) {
            let _ = fs::remove_file(dir.join("blobs").join(sha)).await;
        }
        Ok(meta)
    }

    /// Unpublishes: deletes the content and keeps a tombstone that answers 410 for a while.
    pub async fn delete(&self, id: &str, auth: &Auth) -> Result<(), ApiError> {
        let mut used = self.used.write().await;
        let dir = self.dir(id);
        let before = held_bytes(&dir).await;
        let meta = self.load(id).await?;
        let result = match &meta {
            None => Err(not_found()),
            Some(_) => match Self::authorize(meta.as_ref(), auth) {
                Ok(()) => self
                    .tombstone(id, self.clock.now())
                    .await
                    .map_err(Into::into),
                Err(e) => Err(e),
            },
        };
        *used = used.saturating_sub(before) + held_bytes(&dir).await;
        result
    }

    async fn tombstone(&self, id: &str, at: u64) -> io::Result<()> {
        let dir = self.dir(id);
        let tomb = Meta {
            updated: self.clock.now(),
            deleted_at: Some(at),
            ..Meta::default()
        };
        let bytes = serde_json::to_vec(&tomb)?;
        write_atomic(&dir.join("meta.json"), &bytes).await?;
        let _ = fs::remove_file(dir.join("manifest")).await;
        let _ = fs::remove_dir_all(dir.join("blobs")).await;
        Ok(())
    }

    /// Expired publications become tombstones, old tombstones and blob-only leftovers go.
    pub async fn sweep(&self) -> SweepStats {
        let mut used = self.used.write().await;
        let now = self.clock.now();
        let mut stats = SweepStats::default();
        for id in ids_in(&self.cfg.data_dir).await {
            let Ok(Some(meta)) = self.read_meta(&id).await else {
                continue;
            };
            let dir = self.dir(&id);
            if let Some(at) = meta.deleted_at {
                if now >= at + self.cfg.tombstone_days * 86_400
                    && fs::remove_dir_all(&dir).await.is_ok()
                {
                    stats.tombstones_removed += 1;
                }
            } else if meta.manifest_sha.is_none() {
                if now >= meta.updated + self.cfg.pending_hours * 3600
                    && fs::remove_dir_all(&dir).await.is_ok()
                {
                    stats.pending_removed += 1;
                }
            } else if meta.expires <= now && self.tombstone(&id, meta.expires).await.is_ok() {
                stats.expired += 1;
            }
        }
        *used = self.scan_used().await;
        stats
    }
}

fn not_found() -> ApiError {
    ApiError::new(StatusCode::NOT_FOUND, "not_found")
}
