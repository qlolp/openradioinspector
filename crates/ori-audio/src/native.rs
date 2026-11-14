use crate::*;
use std::sync::Arc;

struct Disabled;
impl AudioBackend for Disabled {
    fn inputs(&self) -> AudioResult<Vec<AudioInput>> {
        Err(AudioError::Disabled)
    }
    fn open(&self, _: &InputSelection, _: u32, _: PcmSink) -> AudioResult<Box<dyn AudioStream>> {
        Err(AudioError::Disabled)
    }
}
pub(crate) fn backend(enabled: bool) -> AudioResult<Arc<dyn AudioBackend>> {
    if !enabled {
        return Ok(Arc::new(Disabled));
    }
    #[cfg(feature = "native")]
    {
        Ok(Arc::new(cpal_backend::Native))
    }
    #[cfg(not(feature = "native"))]
    {
        Err(AudioError::NativeUnavailable)
    }
}

#[cfg(feature = "native")]
mod cpal_backend {
    use super::*;
    use chrono::{SecondsFormat, Utc};
    use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
    use std::time::Duration;

    pub(super) struct Native;
    fn error(error: cpal::Error) -> AudioError {
        AudioError::Backend(error.to_string().chars().take(256).collect())
    }
    fn formats(device: &cpal::Device) -> AudioResult<Vec<AudioFormatRange>> {
        let mut formats = Vec::new();
        for config in device.supported_input_configs().map_err(error)? {
            if formats.len() == 256 {
                return Err(AudioError::Backend("too many host audio formats".into()));
            }
            formats.push(AudioFormatRange {
                channels: config.channels(),
                encoding: match config.sample_format() {
                    cpal::SampleFormat::I16 => SampleEncoding::I16,
                    cpal::SampleFormat::F32 => SampleEncoding::F32,
                    other => SampleEncoding::Other(format!("{other:?}")),
                },
                min_sample_rate_hz: config.min_sample_rate(),
                max_sample_rate_hz: config.max_sample_rate(),
            });
        }
        Ok(formats)
    }
    fn name(device: &cpal::Device) -> AudioResult<String> {
        let name = device.description().map_err(error)?.name().to_owned();
        if name.len() > 4096 {
            return Err(AudioError::Backend(
                "host input name exceeds descriptor limit".into(),
            ));
        }
        Ok(name)
    }
    impl AudioBackend for Native {
        fn inputs(&self) -> AudioResult<Vec<AudioInput>> {
            // Invoked explicitly by the user. Never call default_input_device or
            // default_input_config: neither proves the selected original format.
            let host = cpal::default_host();
            let mut inputs = Vec::new();
            for (index, device) in host.input_devices().map_err(error)?.enumerate() {
                if index >= 1024 {
                    return Err(AudioError::Backend("too many host audio inputs".into()));
                }
                inputs.push(AudioInput {
                    index: index as u32,
                    name: name(&device)?,
                    formats: formats(&device)?,
                });
            }
            Ok(inputs)
        }
        fn open(
            &self,
            input: &InputSelection,
            rate: u32,
            sink: PcmSink,
        ) -> AudioResult<Box<dyn AudioStream>> {
            let host = cpal::default_host();
            let device = host
                .input_devices()
                .map_err(error)?
                .nth(input.index as usize)
                .ok_or(AudioError::InputUnavailable)?;
            if name(&device)?.as_bytes() != input.name.as_bytes() {
                return Err(AudioError::InputUnavailable);
            }
            if !formats(&device)?.iter().any(|f| {
                f.channels == 1
                    && f.encoding == SampleEncoding::I16
                    && f.min_sample_rate_hz <= rate
                    && rate <= f.max_sample_rate_hz
            }) {
                return Err(AudioError::UnsupportedFormat);
            }
            let config = cpal::StreamConfig {
                channels: 1,
                sample_rate: rate,
                buffer_size: cpal::BufferSize::Default,
            };
            let errors = sink.clone();
            let callback = sink.clone();
            sink.revalidate()?;
            let stream = device
                .build_input_stream::<i16, _, _>(
                    config,
                    move |samples, info| {
                        if !callback.is_accepting() {
                            return;
                        }
                        // Host callback clock -> UTC. The interval's duration remains
                        // exact frames/rate; wall-clock alignment is host timing, not RF
                        // or ADC synchronization proof.
                        let timestamp = info.timestamp();
                        let Some(delay) =
                            timestamp.callback.checked_duration_since(timestamp.capture)
                        else {
                            callback.fail(AudioError::Clock);
                            return;
                        };
                        let Ok(delay) = chrono::TimeDelta::from_std(delay) else {
                            callback.fail(AudioError::Clock);
                            return;
                        };
                        let Some(captured) = Utc::now().checked_sub_signed(delay) else {
                            callback.fail(AudioError::Clock);
                            return;
                        };
                        let Ok(started) = captured
                            .to_rfc3339_opts(SecondsFormat::Micros, false)
                            .parse()
                        else {
                            callback.fail(AudioError::Clock);
                            return;
                        };
                        for (index, chunk) in
                            samples.chunks(callback.max_block_frames()).enumerate()
                        {
                            let Ok(started_at) = crate::actor::add_frames(
                                &started,
                                (index * callback.max_block_frames()) as u64,
                                rate,
                            ) else {
                                callback.fail(AudioError::Clock);
                                return;
                            };
                            let mut bytes = Vec::with_capacity(chunk.len() * 2);
                            for sample in chunk {
                                bytes.extend_from_slice(&sample.to_le_bytes());
                            }
                            match callback.push_pcm16(&bytes, started_at) {
                                PushOutcome::Accepted | PushOutcome::Discarded => {}
                                PushOutcome::Dropped | PushOutcome::Rejected => break,
                            }
                        }
                    },
                    move |failure| errors.fail(error(failure)),
                    Some(Duration::from_secs(5)),
                )
                .map_err(error)?;
            Ok(Box::new(NativeStream::new(stream, |stream| {
                stream.play().map_err(error)
            })))
        }
    }
    struct NativeStream<S> {
        stream: Option<S>,
        play: fn(&S) -> AudioResult<()>,
    }
    impl<S> NativeStream<S> {
        fn new(stream: S, play: fn(&S) -> AudioResult<()>) -> Self {
            Self {
                stream: Some(stream),
                play,
            }
        }
    }
    impl<S: 'static> AudioStream for NativeStream<S> {
        fn start(&mut self) -> AudioResult<()> {
            (self.play)(self.stream.as_ref().ok_or(AudioError::Closed)?)
        }
        fn stop(&mut self) -> AudioResult<()> {
            // CPAL ALSA pause() requires an optional hardware capability. Drop
            // instead releases the input and joins its callback worker without
            // requiring pause support; Core needs release rather than pause.
            drop(self.stream.take());
            Ok(())
        }
    }

    #[cfg(test)]
    mod tests {
        use super::NativeStream;
        use crate::AudioStream;
        use std::sync::{
            Arc,
            atomic::{AtomicUsize, Ordering},
        };
        struct DropOnlyInput(Arc<AtomicUsize>);
        impl Drop for DropOnlyInput {
            fn drop(&mut self) {
                self.0.fetch_add(1, Ordering::AcqRel);
            }
        }
        #[test]
        fn native_stop_releases_owned_input_without_requiring_pause_support() {
            let released = Arc::new(AtomicUsize::new(0));
            let mut stream = NativeStream::new(DropOnlyInput(released.clone()), |_| Ok(()));
            stream.start().unwrap();
            stream.stop().unwrap();
            assert_eq!(released.load(Ordering::Acquire), 1);
            drop(stream);
            assert_eq!(released.load(Ordering::Acquire), 1);
        }
    }
}
