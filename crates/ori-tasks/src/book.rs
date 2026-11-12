use crate::{ControlTask, TaskError, TaskResult};
use fs2::FileExt;
use serde::Serialize;
use serde_json::{Value, json};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
};

pub(crate) struct BookLock(File);
impl BookLock {
    pub(crate) fn acquire(path: &Path) -> TaskResult<Self> {
        let parent = parent(path);
        fs::create_dir_all(parent).map_err(|_| TaskError::io())?;
        let mut name = path.file_name().ok_or_else(TaskError::io)?.to_os_string();
        name.push(".lock");
        let mut options = OpenOptions::new();
        options.read(true).write(true).create(true).truncate(false);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let file = options
            .open(parent.join(name))
            .map_err(|_| TaskError::io())?;
        file.try_lock_exclusive().map_err(|_| {
            TaskError::new(
                503,
                "task_book_locked",
                format!(
                    "TaskBook уже используется другим scheduler: {}",
                    path.display()
                ),
            )
        })?;
        Ok(Self(file))
    }
}
impl Drop for BookLock {
    fn drop(&mut self) {
        let _ = FileExt::unlock(&self.0);
    }
}
fn parent(path: &Path) -> &Path {
    path.parent()
        .filter(|v| !v.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."))
}
pub(crate) struct Book {
    pub tasks: Vec<ControlTask>,
    path: PathBuf,
    maximum: usize,
    max_tasks: usize,
}
#[derive(Serialize)]
struct Payload<'a> {
    format: &'static str,
    version: u8,
    tasks: &'a [ControlTask],
}
impl Book {
    pub(crate) fn load(path: PathBuf, maximum: usize, max_tasks: usize) -> TaskResult<Self> {
        let mut book = Self {
            tasks: Vec::new(),
            path,
            maximum,
            max_tasks,
        };
        let file = match File::open(&book.path) {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(book),
            Err(_) => return Err(TaskError::io()),
        };
        if file.metadata().map_err(|_| TaskError::io())?.len() > maximum as u64 {
            return Err(TaskError::resource());
        }
        let mut bytes = Vec::new();
        file.take(maximum as u64 + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| TaskError::io())?;
        if bytes.len() > maximum {
            return Err(TaskError::resource());
        }
        let value: Value = serde_json::from_slice(&bytes).map_err(|_| TaskError::book_invalid())?;
        let object = value.as_object().ok_or_else(TaskError::book_invalid)?;
        let tasks = match object.get("tasks") {
            None => Vec::new(),
            Some(value) => value
                .as_array()
                .ok_or_else(TaskError::book_invalid)?
                .clone(),
        };
        if tasks.len() > max_tasks {
            return Err(TaskError::resource());
        }
        book.tasks = tasks
            .into_iter()
            .map(ControlTask::from_dict)
            .collect::<TaskResult<Vec<_>>>()?;
        // Defaults can expand a sparse legacy file. Bound canonical state too.
        book.bytes()?;
        Ok(book)
    }
    fn payload(&self) -> Payload<'_> {
        Payload {
            format: "openradioinspector-task-book",
            version: 1,
            tasks: &self.tasks,
        }
    }
    fn bytes(&self) -> TaskResult<Vec<u8>> {
        let mut writer = Bounded {
            bytes: Vec::new(),
            maximum: self.maximum,
            exceeded: false,
        };
        if serde_json::to_writer_pretty(&mut writer, &self.payload()).is_err() {
            return Err(if writer.exceeded {
                TaskError::resource()
            } else {
                TaskError::book_invalid()
            });
        }
        Ok(writer.bytes)
    }
    pub(crate) fn add(&mut self, task: ControlTask) -> TaskResult<()> {
        if self.tasks.len() >= self.max_tasks {
            return Err(TaskError::resource());
        }
        self.tasks.push(task);
        if let Err(error) = self.bytes() {
            self.tasks.pop();
            return Err(error);
        }
        Ok(())
    }
    pub(crate) fn save(&self) -> TaskResult<()> {
        let bytes = self.bytes()?;
        let parent = parent(&self.path);
        fs::create_dir_all(parent).map_err(|_| TaskError::io())?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::{MetadataExt, PermissionsExt};
            let metadata = fs::symlink_metadata(parent).map_err(|_| TaskError::io())?;
            // Same authority boundary: final parent must be owned and not writable by others.
            if metadata.file_type().is_symlink()
                || metadata.uid() != unsafe { libc::getuid() }
                || metadata.permissions().mode() & 0o022 != 0
            {
                return Err(TaskError::io());
            }
        }
        let name = self
            .path
            .file_name()
            .ok_or_else(TaskError::io)?
            .to_string_lossy();
        let mut temporary = tempfile::Builder::new()
            .prefix(&format!(".{name}."))
            .suffix(".tmp")
            .tempfile_in(parent)
            .map_err(|_| TaskError::io())?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = match fs::metadata(&self.path) {
                Ok(metadata) => metadata.permissions().mode() & 0o7777,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => 0o600,
                Err(_) => return Err(TaskError::io()),
            };
            temporary
                .as_file()
                .set_permissions(fs::Permissions::from_mode(mode))
                .map_err(|_| TaskError::io())?;
        }
        temporary
            .write_all(&bytes)
            .and_then(|()| temporary.flush())
            .and_then(|()| temporary.as_file().sync_all())
            .map_err(|_| TaskError::io())?;
        let source = temporary.into_temp_path();
        publish(&source, &self.path)?;
        #[cfg(unix)]
        File::open(parent)
            .and_then(|file| file.sync_all())
            .map_err(|_| TaskError::io())?;
        Ok(())
    }
    pub(crate) fn public(&self) -> TaskResult<Value> {
        self.tasks
            .iter()
            .map(ControlTask::public)
            .collect::<TaskResult<Vec<_>>>()
            .map(|v| json!(v))
    }
}
#[cfg(not(windows))]
fn publish(source: &Path, target: &Path) -> TaskResult<()> {
    fs::rename(source, target).map_err(|_| TaskError::io())
}
#[cfg(windows)]
fn publish(source: &Path, target: &Path) -> TaskResult<()> {
    use std::os::windows::ffi::OsStrExt;
    use winapi::um::{
        winbase::MoveFileExW,
        winbase::{MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH},
    };
    let source: Vec<u16> = source.as_os_str().encode_wide().chain(Some(0)).collect();
    let target: Vec<u16> = target.as_os_str().encode_wide().chain(Some(0)).collect();
    // Windows has no portable POSIX directory fsync. Request write-through
    // atomic replacement after FlushFileBuffers on the complete source file.
    if unsafe {
        MoveFileExW(
            source.as_ptr(),
            target.as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    } == 0
    {
        return Err(TaskError::io());
    }
    Ok(())
}
struct Bounded {
    bytes: Vec<u8>,
    maximum: usize,
    exceeded: bool,
}
impl Write for Bounded {
    fn write(&mut self, input: &[u8]) -> std::io::Result<usize> {
        if input.len() > self.maximum.saturating_sub(self.bytes.len()) {
            self.exceeded = true;
            return Err(std::io::Error::other("TaskBook byte budget"));
        }
        self.bytes.extend_from_slice(input);
        Ok(input.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
