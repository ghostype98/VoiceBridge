/**
 * AudioWorklet处理器 - 音频重采样和PCM编码
 * 功能：
 * 1. 接收麦克风音频流（任意采样率）
 * 2. 重采样到24kHz
 * 3. 转换为PCM 16-bit单声道
 * 4. 分块输出（2048样本/块）
 */

class AudioResamplerProcessor extends AudioWorkletProcessor {
    constructor(options) {
        super();
        
        // 配置参数
        // 从 processorOptions 获取目标采样率，默认为 16000（ASR要求）
        const processorOptions = (options && options.processorOptions) || {};
        this.targetSampleRate = processorOptions.targetSampleRate || 16000;
        this.chunkSize = processorOptions.chunkSize || 2048; // 块大小：2048样本
        this.bitDepth = 16; // 16-bit
        
        // 状态变量
        this.inputBuffer = [];
        this.outputBuffer = [];
        this.lastSampleRate = null;
        this.resampleRatio = null;
        
        // 统计信息
        this.processedSamples = 0;
        this.outputChunks = 0;
        this.lastDebugTimestamp = 0;
        
        // 初始化
        this.port.postMessage({
            type: 'initialized',
            config: {
                targetSampleRate: this.targetSampleRate,
                chunkSize: this.chunkSize,
                bitDepth: this.bitDepth
            }
        });
    }
    
    /**
     * 线性重采样
     * @param {Float32Array} input - 输入音频数据
     * @param {number} inputSampleRate - 输入采样率
     * @param {number} outputSampleRate - 输出采样率
     * @returns {Float32Array} 重采样后的音频数据
     */
    resample(input, inputSampleRate, outputSampleRate) {
        if (inputSampleRate === outputSampleRate) {
            return input;
        }
        
        const ratio = inputSampleRate / outputSampleRate;
        const outputLength = Math.floor(input.length / ratio);
        const output = new Float32Array(outputLength);
        
        for (let i = 0; i < outputLength; i++) {
            const srcIndex = i * ratio;
            const index = Math.floor(srcIndex);
            const fraction = srcIndex - index;
            
            if (index + 1 < input.length) {
                // 线性插值
                output[i] = input[index] * (1 - fraction) + input[index + 1] * fraction;
            } else {
                output[i] = input[index] || 0;
            }
        }
        
        return output;
    }
    
    /**
     * 转换为PCM 16-bit
     * @param {Float32Array} float32Array - 浮点音频数据（-1.0到1.0）
     * @returns {Int16Array} 16-bit PCM数据
     */
    floatTo16BitPCM(float32Array) {
        const int16Array = new Int16Array(float32Array.length);
        for (let i = 0; i < float32Array.length; i++) {
            // 限制范围到[-1, 1]
            const sample = Math.max(-1, Math.min(1, float32Array[i]));
            // 转换为16-bit整数（-32768到32767）
            int16Array[i] = sample < 0 ? sample * 0x8000 : sample * 0x7FFF;
        }
        return int16Array;
    }
    
    /**
     * 处理音频数据
     * @param {Array} inputs - 输入音频数据
     * @param {Array} outputs - 输出音频数据（可选）
     * @param {Object} parameters - 参数对象
     * @returns {boolean} 是否继续处理
     */
    process(inputs, outputs, parameters) {
        const input = inputs[0];
        
        // 检查是否有输入
        if (!input || input.length === 0) {
            return true; // 继续运行
        }
        
        const inputChannel = input[0]; // 取第一个声道（单声道）
        if (!inputChannel || inputChannel.length === 0) {
            return true;
        }
        
        // 获取当前采样率（从AudioContext的sampleRate）
        // 注意：在AudioWorklet中，sampleRate是全局变量，表示AudioContext的采样率
        const currentSampleRate = typeof sampleRate !== 'undefined' ? sampleRate : 48000;
        
        // 如果采样率变化，重新计算重采样比例
        if (this.lastSampleRate !== currentSampleRate) {
            this.lastSampleRate = currentSampleRate;
            this.resampleRatio = currentSampleRate / this.targetSampleRate;
            this.port.postMessage({
                type: 'sampleRateChanged',
                from: currentSampleRate,
                to: this.targetSampleRate,
                ratio: this.resampleRatio
            });
        }
        
        // 将输入数据添加到缓冲区
        this.inputBuffer.push(...inputChannel);
        
        // 如果采样率不匹配，需要重采样
        let processedData;
        if (currentSampleRate !== this.targetSampleRate) {
            // 重采样
            const float32Input = new Float32Array(this.inputBuffer);
            processedData = this.resample(float32Input, currentSampleRate, this.targetSampleRate);
            this.inputBuffer = [];
        } else {
            // 直接使用
            processedData = new Float32Array(this.inputBuffer);
            this.inputBuffer = [];
        }
        
        // 添加到输出缓冲区
        this.outputBuffer.push(...processedData);
        
        // 当缓冲区达到块大小时，输出一个块
        while (this.outputBuffer.length >= this.chunkSize) {
            // 提取一个块
            const chunk = this.outputBuffer.splice(0, this.chunkSize);
            
            // 转换为PCM 16-bit
            const pcmData = this.floatTo16BitPCM(chunk);
            
            // 转换为ArrayBuffer（小端序）
            const buffer = new ArrayBuffer(pcmData.length * 2);
            const view = new DataView(buffer);
            for (let i = 0; i < pcmData.length; i++) {
                view.setInt16(i * 2, pcmData[i], true); // true表示小端序
            }
            
            // 发送到主线程
            const ts = currentFrame / currentSampleRate;
            this.port.postMessage({
                type: 'audioChunk',
                data: buffer,
                sampleRate: this.targetSampleRate,
                samples: pcmData.length,
                timestamp: ts
            }, [buffer]); // 转移所有权
            
            this.outputChunks++;
            this.processedSamples += pcmData.length;

            // 调试：周期性输出块信息，便于检查是否存在不规则的时间间隔 / 块大小
            if (this.outputChunks % 50 === 0) {
                this.port.postMessage({
                    type: 'debug',
                    chunks: this.outputChunks,
                    processedSamples: this.processedSamples,
                    timestamp: ts,
                    sampleRate: this.targetSampleRate,
                    chunkSize: this.chunkSize
                });
            }
        }
        
        return true; // 继续处理
    }
}

// 注册AudioWorklet处理器
registerProcessor('audio-resampler-processor', AudioResamplerProcessor);
