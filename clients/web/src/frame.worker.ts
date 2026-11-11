import { parseFrame } from './pipeline';
self.onmessage = (message: MessageEvent<{ raw: string; generation: number }>) => {
  try {
    self.postMessage({ frame: parseFrame(message.data.raw), generation: message.data.generation });
  } catch (error) {
    self.postMessage({
      error: error instanceof Error ? error.message : 'Invalid committed event',
      generation: message.data.generation,
    });
  }
};
