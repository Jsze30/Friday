import AVFAudio
import Foundation
import LiveKit
import os

/// Receives LiveKit's echo-cancelled microphone capture.
///
/// While Friday sleeps, this processor copies audio for local openWakeWord
/// scoring and replaces the outgoing buffer with silence. After wake, it
/// forwards microphone audio unchanged and retains the preceding two seconds
/// for the agent's pre-roll.
final class WakeDetector: NSObject, AudioCustomProcessingDelegate, @unchecked Sendable {
    static let sampleRate: Double = 16_000
    static let blockSamples = 1_280
    static let windowSeconds: Double = 2.0

    private let onAudioChunk: @Sendable (Data) -> Void
    private let lock = NSLock()
    private var converter: AVAudioConverter?
    private var converterInputFormat: AVAudioFormat?
    private var ring: [Int16]
    private var writeIndex = 0
    private var filled = 0
    private var pending: [Int16] = []
    private var paused = false
    private var framesReceived = 0

    private static let logger = Logger(
        subsystem: "com.friday.menubar",
        category: "wake"
    )

    init(onAudioChunk: @escaping @Sendable (Data) -> Void) {
        self.onAudioChunk = onAudioChunk
        ring = [Int16](
            repeating: 0,
            count: Int(Self.sampleRate * Self.windowSeconds)
        )
        pending.reserveCapacity(Self.blockSamples * 2)
    }

    /// Stop wake scoring and return all captured pre-roll in chronological order.
    func pauseAndTakePreRoll() -> Data {
        lock.lock()
        paused = true
        let samples = linearizedRingLocked()
        pending.removeAll(keepingCapacity: true)
        lock.unlock()
        return samples.withUnsafeBufferPointer { Data(buffer: $0) }
    }

    /// Resume local wake scoring with a clean temporal state.
    func resume() {
        lock.lock()
        writeIndex = 0
        filled = 0
        pending.removeAll(keepingCapacity: true)
        paused = false
        lock.unlock()
    }

    // MARK: - AudioCustomProcessingDelegate

    func audioProcessingInitialize(sampleRate: Int, channels: Int) {
        Self.logger.info(
            "capture initialized rate=\(sampleRate)Hz channels=\(channels)"
        )
    }

    func audioProcessingProcess(audioBuffer: LKAudioBuffer) {
        guard let pcmBuffer = audioBuffer.toAVAudioPCMBuffer(),
              let samples = convertTo16k(pcmBuffer)
        else {
            silenceIfSleeping(audioBuffer)
            return
        }

        var chunks: [Data] = []
        lock.lock()
        framesReceived += 1
        if framesReceived == 1 || framesReceived % 30_000 == 0 {
            Self.logger.info(
                "capture alive buffers=\(self.framesReceived) format=\(pcmBuffer.format.sampleRate)Hz forwarding=\(self.paused)"
            )
        }

        let shouldSilence = !paused
        if shouldSilence {
            appendToRingLocked(samples)
        }
        // Keep the localhost stream active during turns. Python remains paused
        // and discards these blocks, but the WebSocket cannot expire while a
        // slow model response is in progress.
        pending.append(contentsOf: samples)
        while pending.count >= Self.blockSamples {
            let block = Array(pending.prefix(Self.blockSamples))
            pending.removeFirst(Self.blockSamples)
            chunks.append(
                block.withUnsafeBufferPointer { Data(buffer: $0) }
            )
        }
        lock.unlock()

        if shouldSilence {
            silence(audioBuffer)
        }
        for chunk in chunks {
            onAudioChunk(chunk)
        }
    }

    func audioProcessingRelease() {
        Self.logger.info("capture released")
    }

    // MARK: - Audio handling

    private func silenceIfSleeping(_ audioBuffer: LKAudioBuffer) {
        lock.lock()
        let shouldSilence = !paused
        lock.unlock()
        if shouldSilence {
            silence(audioBuffer)
        }
    }

    private func silence(_ audioBuffer: LKAudioBuffer) {
        for channelIndex in 0..<audioBuffer.channels {
            let channel = audioBuffer.rawBuffer(forChannel: channelIndex)
            channel.update(repeating: 0, count: audioBuffer.frames)
        }
    }

    /// Caller must hold `lock`.
    private func appendToRingLocked(_ samples: [Int16]) {
        for sample in samples {
            ring[writeIndex] = sample
            writeIndex = (writeIndex + 1) % ring.count
        }
        filled = min(filled + samples.count, ring.count)
    }

    /// Caller must hold `lock`.
    private func linearizedRingLocked() -> [Int16] {
        guard filled == ring.count else {
            return Array(ring.prefix(filled))
        }
        var output = [Int16](repeating: 0, count: ring.count)
        let tail = ring.count - writeIndex
        output[0..<tail] = ring[writeIndex..<ring.count]
        if writeIndex > 0 {
            output[tail...] = ring[0..<writeIndex]
        }
        return output
    }

    private func convertTo16k(_ buffer: AVAudioPCMBuffer) -> [Int16]? {
        lock.lock()
        if converter == nil || converterInputFormat != buffer.format {
            guard let target = AVAudioFormat(
                commonFormat: .pcmFormatInt16,
                sampleRate: Self.sampleRate,
                channels: 1,
                interleaved: true
            ), let newConverter = AVAudioConverter(
                from: buffer.format,
                to: target
            ) else {
                lock.unlock()
                return nil
            }
            converter = newConverter
            converterInputFormat = buffer.format
        }
        guard let converter else {
            lock.unlock()
            return nil
        }
        let targetFormat = converter.outputFormat
        lock.unlock()

        let ratio = targetFormat.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(
            ceil(Double(buffer.frameLength) * ratio)
        ) + 8
        guard let output = AVAudioPCMBuffer(
            pcmFormat: targetFormat,
            frameCapacity: capacity
        ) else {
            return nil
        }

        var consumed = false
        var error: NSError?
        let status = converter.convert(to: output, error: &error) {
            _, outputStatus in
            if consumed {
                outputStatus.pointee = .noDataNow
                return nil
            }
            consumed = true
            outputStatus.pointee = .haveData
            return buffer
        }
        guard status != .error,
              error == nil,
              let channel = output.int16ChannelData
        else {
            return nil
        }
        return Array(
            UnsafeBufferPointer(
                start: channel[0],
                count: Int(output.frameLength)
            )
        )
    }
}
