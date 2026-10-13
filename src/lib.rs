//! Python bindings for fakeprint: the fake WSD printer, run in-process.
//!
//! `Server` is a thin owner of `fakeprint::server::serve(ServerConfig::embedded(..))` on its own
//! thread and tokio runtime, bound to a loopback port. The library does the rest: routes, the
//! `/v1` control plane, job timers, event delivery, reply validation and a graceful shutdown.
//! WS-Discovery, JetDirect :9100, the Windows named pipe and the instance lock are never started.
//!
//! The binding adds no routes and no handlers. The control plane is reached in-process by driving
//! the library's own `ServerHandle::router()` (the manager router: `/v1/devices`, `/d/{id}/...` and the
//! legacy default-device aliases; see `control`), so the JSON is exactly what `/v1/*` serves.
//! Several printers are one `ServerConfig::with_devices(..)`; the per-printer calls take an
//! optional `device` id (default: the first device).
//! Fault injection and runtime timing are the library's own server-side rules (`/v1/faults`
//! `inject`, `/v1/timing`), reached through `control`; the Python layer only shapes the JSON.

use std::net::{IpAddr, SocketAddr};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::Duration;

use axum::body::Body;
use axum::http::{Method, Request};
use axum::Router;
use fakeprint::manager::DeviceManager;
use fakeprint::profile::PrinterProfile;
use fakeprint::server::{serve, DeviceConfig, ServerConfig};
use fakeprint::wsd::jobs::CancelSource;
use fakeprint::wsd::validate::ReplyValidator;
use fakeprint::wsd::SharedWorld;
use pyo3::exceptions::{PyKeyError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyTuple};
use tokio::sync::oneshot;
use tower::ServiceExt;
use uuid::Uuid;

/// What the server thread reports once it is listening.
struct Ready {
    addr: SocketAddr,
    base_url: String,
    device_endpoint: String,
    service_endpoint: String,
    world: SharedWorld,
    validator: Arc<ReplyValidator>,
    device_uuid: Uuid,
    ipp_uuid: Uuid,
    inbox: PathBuf,
    /// The directory this binding created (the parent of every device inbox) or was given.
    inbox_root: PathBuf,
    manager: Arc<DeviceManager>,
    router: Router,
    /// `(id, uuid, ipp_uuid, profile, friendly_name, inbox_dir)` per device at start-up.
    devices: Vec<(String, String, String, String, String, String)>,
}

#[pyclass(module = "fakeprint")]
struct Server {
    ready: Ready,
    own_inbox: bool,
    stop: Mutex<Option<oneshot::Sender<()>>>,
    thread: Mutex<Option<JoinHandle<anyhow::Result<()>>>>,
}

fn lock<T>(m: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|p| p.into_inner())
}

fn rt_err(e: impl std::fmt::Display) -> PyErr {
    PyRuntimeError::new_err(e.to_string())
}

/// One device description from Python: `(id, profile, uuid_or_None, friendly_name_or_None)`.
type DeviceSpec = (String, String, Option<String>, Option<String>);

fn device_configs(specs: Vec<DeviceSpec>) -> PyResult<Vec<DeviceConfig>> {
    specs
        .into_iter()
        .map(|(id, profile, uuid, name)| {
            let mut dc = match uuid {
                Some(u) => {
                    let u = Uuid::parse_str(&u)
                        .map_err(|e| PyValueError::new_err(format!("device {id:?} uuid: {e}")))?;
                    DeviceConfig::new(id, profile, u)
                }
                None => DeviceConfig::derived(id, profile),
            };
            dc.friendly_name = name;
            Ok(dc)
        })
        .collect()
}

fn get_u64(d: &Bound<'_, PyDict>, k: &str) -> PyResult<Option<u64>> {
    d.get_item(k)?.map(|v| v.extract::<u64>()).transpose()
}

fn apply_limits(cfg: &mut ServerConfig, d: &Bound<'_, PyDict>) -> PyResult<()> {
    for (k, _) in d.iter() {
        let k: String = k.extract()?;
        if !matches!(
            k.as_str(),
            "max_document"
                | "max_body"
                | "max_documents_per_job"
                | "max_active_jobs"
                | "history_cap"
                | "max_name"
                | "max_requested_names"
                | "max_condition_history"
        ) {
            return Err(PyValueError::new_err(format!("unknown limit {k:?}")));
        }
    }
    let l = &mut cfg.limits;
    if let Some(v) = get_u64(d, "max_document")? {
        l.max_document = v as usize;
    }
    if let Some(v) = get_u64(d, "max_body")? {
        l.max_body = v as usize;
    }
    if let Some(v) = get_u64(d, "max_documents_per_job")? {
        l.max_documents_per_job = v as u32;
    }
    if let Some(v) = get_u64(d, "max_active_jobs")? {
        l.max_active_jobs = v as usize;
    }
    if let Some(v) = get_u64(d, "history_cap")? {
        l.history_cap = v as usize;
    }
    if let Some(v) = get_u64(d, "max_name")? {
        l.max_name = v as usize;
    }
    if let Some(v) = get_u64(d, "max_requested_names")? {
        l.max_requested_names = v as usize;
    }
    if let Some(v) = get_u64(d, "max_condition_history")? {
        l.max_condition_history = v as usize;
    }
    Ok(())
}

fn apply_timing(cfg: &mut ServerConfig, d: &Bound<'_, PyDict>) -> PyResult<()> {
    for (k, _) in d.iter() {
        let k: String = k.extract()?;
        if !matches!(
            k.as_str(),
            "document_timeout_ms"
                | "terminating_ms"
                | "processing_base_ms"
                | "processing_per_mib_ms"
                | "document_busy_ms"
                | "bytes_per_sheet"
        ) {
            return Err(PyValueError::new_err(format!("unknown timing {k:?}")));
        }
    }
    let t = &mut cfg.timing;
    let ms = Duration::from_millis;
    if let Some(v) = get_u64(d, "document_timeout_ms")? {
        t.document_timeout = ms(v);
    }
    if let Some(v) = get_u64(d, "terminating_ms")? {
        t.terminating = ms(v);
    }
    if let Some(v) = get_u64(d, "processing_base_ms")? {
        t.processing_base = ms(v);
    }
    if let Some(v) = get_u64(d, "processing_per_mib_ms")? {
        t.processing_per_mib = ms(v);
    }
    if let Some(v) = get_u64(d, "document_busy_ms")? {
        t.document_busy = ms(v);
    }
    if let Some(v) = get_u64(d, "bytes_per_sheet")? {
        t.bytes_per_sheet = v;
    }
    Ok(())
}

impl Server {
    /// The world of `device` (default: the first device). `KeyError` for an unknown id.
    fn world(&self, device: Option<&str>) -> PyResult<SharedWorld> {
        match device {
            None => Ok(Arc::clone(&self.ready.world)),
            Some(id) => self
                .ready
                .manager
                .world_of(id)
                .ok_or_else(|| PyKeyError::new_err(format!("unknown device {id:?}"))),
        }
    }

    fn validator(&self, device: Option<&str>) -> PyResult<Arc<ReplyValidator>> {
        match device {
            None => Ok(Arc::clone(&self.ready.validator)),
            Some(_) => Ok(Arc::clone(
                &self.world(device)?.blocking_read().print_ctx().validator,
            )),
        }
    }
}

#[pymethods]
impl Server {
    /// Start the server and return once it is listening (loopback only).
    #[new]
    #[pyo3(signature = (*, profiles=None, profile="generic-wsd", host="127.0.0.1", port=0, device_uuid=None, ipp_uuid=None, inbox=None, name=None, wsd=true, ipp=true, validate_replies=true, limits=None, timing=None, initial_events=false, event_rate_secs=None, tick_ms=None, devices=None))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        py: Python<'_>,
        profiles: Option<PathBuf>,
        profile: &str,
        host: &str,
        port: u16,
        device_uuid: Option<&str>,
        ipp_uuid: Option<&str>,
        inbox: Option<PathBuf>,
        name: Option<&str>,
        wsd: bool,
        ipp: bool,
        validate_replies: bool,
        limits: Option<Bound<'_, PyDict>>,
        timing: Option<Bound<'_, PyDict>>,
        initial_events: bool,
        event_rate_secs: Option<u32>,
        tick_ms: Option<u64>,
        devices: Option<Vec<DeviceSpec>>,
    ) -> PyResult<Self> {
        let ip: IpAddr = host
            .parse()
            .map_err(|e| PyValueError::new_err(format!("host {host}: {e}")))?;
        if !ip.is_loopback() {
            return Err(PyValueError::new_err(format!(
                "host {host}: embedded servers bind loopback only"
            )));
        }
        let parse_uuid = |what: &str, t: Option<&str>| -> PyResult<Option<Uuid>> {
            t.map(|t| Uuid::parse_str(t).map_err(|e| PyValueError::new_err(format!("{what}: {e}"))))
                .transpose()
        };
        let own_inbox = inbox.is_none();
        let inbox = inbox.unwrap_or_else(|| {
            std::env::temp_dir().join(format!("fakeprint-embedded-{}", Uuid::new_v4()))
        });

        let mut cfg = ServerConfig::embedded(inbox.clone());
        if let Some(specs) = devices.filter(|d| !d.is_empty()) {
            cfg = cfg.with_devices(device_configs(specs)?);
        }
        cfg.listen = SocketAddr::new(ip, port);
        cfg.profile_dir = profiles;
        cfg.profile_name = profile.to_string();
        if let Some(u) = parse_uuid("device_uuid", device_uuid)? {
            cfg.device_uuid = u;
        }
        cfg.ipp_uuid = parse_uuid("ipp_uuid", ipp_uuid)?;
        if let Some(n) = name {
            cfg.lab_name = n.to_string();
        }
        cfg.wsd = wsd;
        cfg.ipp = ipp;
        cfg.validate_replies = Some(validate_replies);
        cfg.initial_events = initial_events;
        cfg.event_rate_secs = event_rate_secs;
        if let Some(t) = tick_ms {
            cfg.tick = Duration::from_millis(t.max(1));
        }
        if let Some(d) = limits {
            apply_limits(&mut cfg, &d)?;
        }
        if let Some(d) = timing {
            apply_timing(&mut cfg, &d)?;
        }

        let root_inbox = inbox.clone();
        let (ready_tx, ready_rx) = oneshot::channel::<Result<Ready, String>>();
        let (stop_tx, stop_rx) = oneshot::channel::<()>();
        let thread = std::thread::Builder::new()
            .name("fakeprint-server".into())
            .spawn(move || -> anyhow::Result<()> {
                let runtime = tokio::runtime::Builder::new_multi_thread()
                    .enable_all()
                    .worker_threads(2)
                    .build()?;
                runtime.block_on(async move {
                    let handle = match serve(cfg).await {
                        Ok(h) => h,
                        Err(e) => {
                            let msg = format!("{e:#}");
                            let _ = ready_tx.send(Err(msg.clone()));
                            return Err(anyhow::anyhow!(msg));
                        }
                    };
                    let devices = handle
                        .devices()
                        .await
                        .into_iter()
                        .map(|d| {
                            (
                                d.id,
                                d.uuid.to_string(),
                                d.ipp_uuid.to_string(),
                                d.profile,
                                d.friendly_name,
                                d.inbox_dir,
                            )
                        })
                        .collect();
                    let _ = ready_tx.send(Ok(Ready {
                        addr: handle.local_addr(),
                        base_url: handle.base_url().to_string(),
                        device_endpoint: handle.device_endpoint().to_string(),
                        service_endpoint: handle.service_endpoint().to_string(),
                        world: handle.world(),
                        validator: handle.validator(),
                        device_uuid: handle.device_uuid(),
                        ipp_uuid: handle.ipp_uuid(),
                        inbox: handle.inbox_dir().to_path_buf(),
                        inbox_root: root_inbox,
                        manager: handle.manager(),
                        router: handle.router(),
                        devices,
                    }));
                    let _ = stop_rx.await;
                    handle.shutdown().await;
                    Ok(())
                })
            })
            .map_err(|e| rt_err(format!("could not start the server thread: {e}")))?;

        match py.allow_threads(|| ready_rx.blocking_recv()) {
            Ok(Ok(ready)) => Ok(Self {
                ready,
                own_inbox,
                stop: Mutex::new(Some(stop_tx)),
                thread: Mutex::new(Some(thread)),
            }),
            Ok(Err(e)) => {
                let _ = thread.join();
                if own_inbox {
                    let _ = std::fs::remove_dir_all(&inbox);
                }
                Err(rt_err(format!("fakeprint server failed to start: {e}")))
            }
            Err(_) => {
                let reason = match thread.join() {
                    Ok(Err(e)) => format!("{e:#}"),
                    Ok(Ok(())) => "exited before listening".to_string(),
                    Err(_) => "panicked while starting".to_string(),
                };
                Err(rt_err(format!(
                    "fakeprint server failed to start: {reason}"
                )))
            }
        }
    }

    #[getter]
    fn port(&self) -> u16 {
        self.ready.addr.port()
    }

    #[getter]
    fn base_url(&self) -> String {
        self.ready.base_url.clone()
    }

    #[getter]
    fn device_endpoint(&self) -> String {
        self.ready.device_endpoint.clone()
    }

    #[getter]
    fn service_endpoint(&self) -> String {
        self.ready.service_endpoint.clone()
    }

    #[getter]
    fn device_uuid(&self) -> String {
        self.ready.device_uuid.to_string()
    }

    #[getter]
    fn ipp_uuid(&self) -> String {
        self.ready.ipp_uuid.to_string()
    }

    #[getter]
    fn inbox_dir(&self) -> String {
        self.ready.inbox.display().to_string()
    }

    /// Every device at start-up: `(id, uuid, ipp_uuid, profile, friendly_name, inbox_dir)`, the
    /// first one being the default device. Later changes: `GET /v1/devices`.
    #[getter]
    fn devices(&self) -> Vec<(String, String, String, String, String, String)> {
        self.ready.devices.clone()
    }

    #[getter]
    fn running(&self) -> bool {
        lock(&self.thread)
            .as_ref()
            .is_some_and(|t| !t.is_finished())
    }

    /// `GET /v1/status` as JSON text (of `device`, default the first device).
    #[pyo3(signature = (device=None))]
    fn status_json(&self, device: Option<&str>) -> PyResult<String> {
        let st = self.world(device)?.blocking_read().status();
        serde_json::to_string(&st).map_err(rt_err)
    }

    /// Captured documents (the `jobs` rows of `/v1/jobs`) as JSON text.
    #[pyo3(signature = (device=None))]
    fn captured_json(&self, device: Option<&str>) -> PyResult<String> {
        let w = self.world(device)?;
        let w = w.blocking_read();
        serde_json::to_string(&w.captured()).map_err(rt_err)
    }

    /// The `print_jobs` rows of `/v1/jobs` (active and history, ascending by id) as JSON text.
    #[pyo3(signature = (device=None))]
    fn print_jobs_json(&self, py: Python<'_>, device: Option<&str>) -> PyResult<String> {
        let path = match device {
            Some(id) => format!("/d/{id}/v1/jobs"),
            None => "/v1/jobs".to_string(),
        };
        let (status, body) = self.control(py, "GET", &path, None)?;
        if status != 200 {
            return Err(rt_err(format!("GET /v1/jobs: {status} {body}")));
        }
        let v: serde_json::Value = serde_json::from_str(&body).map_err(rt_err)?;
        serde_json::to_string(v.get("print_jobs").unwrap_or(&serde_json::Value::Null))
            .map_err(rt_err)
    }

    /// Call one of the library's control routes in-process: `(http_status, body_text)`.
    /// `body` is JSON text for POST/DELETE. Same handlers as `/v1/*` over HTTP.
    #[pyo3(signature = (method, path, body=None))]
    fn control(
        &self,
        py: Python<'_>,
        method: &str,
        path: &str,
        body: Option<&str>,
    ) -> PyResult<(u16, String)> {
        let method = Method::from_bytes(method.as_bytes())
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        let mut rb = Request::builder().method(method).uri(path);
        let body = match body {
            Some(b) => {
                rb = rb.header("content-type", "application/json");
                Body::from(b.to_string())
            }
            None => Body::empty(),
        };
        let req = rb
            .body(body)
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        let app = self.ready.router.clone();
        py.allow_threads(move || -> PyResult<(u16, String)> {
            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .map_err(rt_err)?;
            rt.block_on(async move {
                let resp = app.oneshot(req).await.map_err(rt_err)?;
                let status = resp.status().as_u16();
                let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
                    .await
                    .map_err(rt_err)?;
                Ok((status, String::from_utf8_lossy(&bytes).into_owned()))
            })
        })
    }

    /// Schema violations found in outgoing messages (newest 64).
    #[pyo3(signature = (device=None))]
    fn validation_problems(&self, device: Option<&str>) -> PyResult<Vec<String>> {
        Ok(self.validator(device)?.recent_problems())
    }

    #[pyo3(signature = (device=None))]
    fn validation_problem_count(&self, device: Option<&str>) -> PyResult<u64> {
        Ok(self.validator(device)?.problem_count())
    }

    /// Bytes of every document captured for `job_id`, in document order.
    #[pyo3(signature = (job_id, device=None))]
    fn documents<'py>(
        &self,
        py: Python<'py>,
        job_id: u32,
        device: Option<&str>,
    ) -> PyResult<Vec<Bound<'py, PyBytes>>> {
        let world = self.world(device)?;
        let mut rows: Vec<(u32, String)> = {
            let w = world.blocking_read();
            w.captured()
                .iter()
                .filter(|c| c.job_id == job_id)
                .map(|c| (c.document_id, c.path.clone()))
                .collect()
        };
        rows.sort_by_key(|r| r.0);
        rows.iter()
            .map(|(_, p)| {
                std::fs::read(p)
                    .map(|b| PyBytes::new(py, &b))
                    .map_err(rt_err)
            })
            .collect()
    }

    /// Simulate the device cancelling a job (no WS-Eventing notification). False if not active.
    ///
    /// Goes through the job engine with `CancelSource::Device` (reason `JobCanceledAtDevice`;
    /// `World::cancel_job` is the remote-user source), then drops the job's captured files like
    /// any successful cancel.
    #[pyo3(signature = (job_id, device=None))]
    fn device_cancel(&self, job_id: u32, device: Option<&str>) -> PyResult<bool> {
        let ctx = self.world(device)?.blocking_read().print_ctx();
        let ok = ctx.engine.cancel(job_id, CancelSource::Device).is_ok();
        if ok {
            if let Some(inbox) = ctx.inbox.as_ref() {
                inbox.discard_job(job_id);
            }
        }
        Ok(ok)
    }

    /// Forget jobs, captures, counters and conditions (inbox files stay).
    #[pyo3(signature = (device=None))]
    fn reset(&self, device: Option<&str>) -> PyResult<()> {
        self.world(device)?.blocking_write().reset();
        Ok(())
    }

    /// Hot-swap the profile by id (from the profiles directory).
    #[pyo3(signature = (id, device=None))]
    fn set_profile(&self, id: &str, device: Option<&str>) -> PyResult<()> {
        self.world(device)?
            .blocking_write()
            .hot_swap_profile(id)
            .map_err(|e| PyKeyError::new_err(format!("{e:#}")))
    }

    /// Install a schema-2 profile document (JSON text) directly.
    #[pyo3(signature = (doc, device=None))]
    fn set_profile_json(&self, doc: &str, device: Option<&str>) -> PyResult<()> {
        let p =
            PrinterProfile::from_json(doc).map_err(|e| PyValueError::new_err(format!("{e:#}")))?;
        self.world(device)?.blocking_write().swap_profile(p);
        Ok(())
    }

    /// Stop serving (graceful: subscriptions end, tasks stop) and wait for the thread.
    fn stop(&self, py: Python<'_>) -> PyResult<()> {
        if let Some(tx) = lock(&self.stop).take() {
            let _ = tx.send(());
        }
        let thread = lock(&self.thread).take();
        let mut res = Ok(());
        if let Some(thread) = thread {
            res = match py.allow_threads(|| thread.join()) {
                Ok(Ok(())) => Ok(()),
                Ok(Err(e)) => Err(rt_err(format!(
                    "fakeprint server stopped with an error: {e:#}"
                ))),
                Err(_) => Err(rt_err("fakeprint server thread panicked")),
            };
        }
        if self.own_inbox {
            let _ = std::fs::remove_dir_all(&self.ready.inbox_root);
        }
        res
    }

    fn __enter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    #[pyo3(signature = (*_args))]
    fn __exit__(&self, py: Python<'_>, _args: &Bound<'_, PyTuple>) -> PyResult<bool> {
        self.stop(py)?;
        Ok(false)
    }

    fn __repr__(&self) -> String {
        format!(
            "Server({}, uuid={})",
            self.ready.base_url, self.ready.device_uuid
        )
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        if let Some(tx) = self.stop.get_mut().ok().and_then(|g| g.take()) {
            let _ = tx.send(());
        }
        // No join: Drop may run while Python holds the GIL; the thread exits on its own.
    }
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_class::<Server>()?;
    Ok(())
}
