/**
 * AudioWorklet processor for mic capture.
 *
 * Accumulates raw float32 samples into fixed-size chunks and transfers each
 * chunk to the main thread via postMessage.  Running off the main thread avoids
 * the timing jitter of the deprecated ScriptProcessorNode and works reliably
 * across all mic hardware (built-in, headset, USB, etc.).
 *
 * chunk size: 512 samples @ 16 kHz  ≈ 32 ms  (good fit for Live API latency)
 */
class MicProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this._chunkSize = (options.processorOptions && options.processorOptions.chunkSize) || 512;
    this._buffer = new Float32Array(this._chunkSize);
    this._offset = 0;
  }

  process(inputs) {
    const channel = inputs[0]?.[0];
    if (!channel) return true;

    for (let i = 0; i < channel.length; i++) {
      this._buffer[this._offset++] = channel[i];

      if (this._offset === this._chunkSize) {
        // Transfer the buffer (zero-copy); create a fresh one immediately after.
        this.port.postMessage(this._buffer, [this._buffer.buffer]);
        this._buffer = new Float32Array(this._chunkSize);
        this._offset = 0;
      }
    }

    return true;
  }
}

registerProcessor("mic-processor", MicProcessor);
