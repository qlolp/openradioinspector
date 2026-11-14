import { parseFrame, type FrameInput } from './pipeline';
self.onmessage = (message: MessageEvent<{ raw: FrameInput; generation: number }>) => {
  try {
    const frame = parseFrame(message.data.raw),
      transfer: ArrayBuffer[] = [];
    if (frame.sweep)
      for (const values of [
        frame.sweep.levels,
        frame.sweep.frequencies_hz,
        frame.sweep.source_indices,
      ])
        if (ArrayBuffer.isView(values)) transfer.push(values.buffer);
    self.postMessage({ frame, generation: message.data.generation }, { transfer });
  } catch (error) {
    self.postMessage({
      error: error instanceof Error ? error.message : 'Invalid committed event',
      generation: message.data.generation,
    });
  }
};
