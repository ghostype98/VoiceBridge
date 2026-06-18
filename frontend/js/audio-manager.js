/**
 * 音频管理器 - 使用Web Audio API和AudioWorklets
 * 功能：
 * 1. 管理麦克风音频捕获
 * 2. 使用AudioWorklet进行音频处理
 * 3. 管理音频队列
 * 4. 实现静默检测
 */

class AudioManager {
    constructor(options = {}) {
        // 配置参数
        this.targetSampleRate = options.targetSampleRate || 24000; // 前端24kHz
        this.sttSampleRate = options.sttSampleRate || 16000; // STT 16kHz
        this.chunkSize = options.chunkSize || 2048; // 块大小
        this.silenceThreshold = options.silenceThreshold || 0.7; // 静默阈值（秒）
        this.maxQueueSize = options.maxQueueSize || 50; // 最大队列大小
        
        // 状态变量
        this.audioContext = null;
        this.mediaStreamSource = null;
        this.workletNode = null;
        this.isRecording = false;
        this.audioQueue = [];
        this.silenceStartTime = null;
        this.lastSoundTime = null;
        
        // 统计信息
        this.stats = {
            totalChunks: 0,
            totalSamples: 0,
            droppedChunks: 0,
            silenceDetections: 0
        };
        
        // 回调函数
        this.onAudioChunk = options.onAudioChunk || null;
        this.onSilenceDetected = options.onSilenceDetected || null;
        this.onError = options.onError || null;
        
        // 工作线程URL
        this.workletUrl = options.workletUrl || '/static/js/audio-processor.js';
    }
    
    /**
     * 初始化音频上下文和AudioWorklet
     */
    async initialize() {
        try {
            // 创建AudioContext
            const AudioContextClass = window.AudioContext || window.webkitAudioContext;
            if (!AudioContextClass) {
                throw new Error('浏览器不支持Web Audio API');
            }
            
            this.audioContext = new AudioContextClass({
                sampleRate: this.targetSampleRate
            });
            
            console.log(`✅ AudioContext创建成功，采样率: ${this.audioContext.sampleRate}Hz`);
            
            // 加载AudioWorklet处理器
            try {
                await this.audioContext.audioWorklet.addModule(this.workletUrl);
                console.log('✅ AudioWorklet处理器加载成功');
            } catch (error) {
                console.error('❌ AudioWorklet加载失败:', error);
                throw new Error(`AudioWorklet加载失败: ${error.message}`);
            }
            
            // 创建AudioWorkletNode - 使用sttSampleRate作为输出采样率（16kHz）
            this.workletNode = new AudioWorkletNode(
                this.audioContext,
                'audio-resampler-processor',
                {
                    processorOptions: {
                        targetSampleRate: this.sttSampleRate || 16000, // 输出16kHz PCM数据
                        chunkSize: this.chunkSize
                    }
                }
            );
            
            // 监听来自处理器的消息
            this.workletNode.port.onmessage = (event) => {
                this.handleWorkletMessage(event.data);
            };
            
            console.log('✅ AudioManager初始化成功');
            
        } catch (error) {
            console.error('❌ AudioManager初始化失败:', error);
            if (this.onError) {
                this.onError(error);
            }
            throw error;
        }
    }
    
    /**
     * 处理来自AudioWorklet的消息
     */
    handleWorkletMessage(data) {
        switch (data.type) {
            case 'initialized':
                console.log('📊 AudioWorklet初始化:', data.config);
                break;
                
            case 'sampleRateChanged':
                console.log(`🔄 采样率变化: ${data.from}Hz -> ${data.to}Hz (比例: ${data.ratio.toFixed(3)})`);
                break;
                
            case 'audioChunk':
                this.handleAudioChunk(data);
                break;
            
            case 'debug':
                // 周期性调试信息：块数、时间戳等，用于检查是否有不规则间隔
                console.log(
                    `🎧 AudioChunk 调试: chunks=${data.chunks}, ` +
                    `samples=${data.processedSamples}, ` +
                    `ts=${data.timestamp.toFixed(3)}s, ` +
                    `sr=${data.sampleRate}, size=${data.chunkSize}`
                );
                break;
                
            default:
                console.log('📨 未知消息类型:', data.type);
        }
    }
    
    /**
     * 处理音频块
     */
    handleAudioChunk(chunkData) {
        const { data, sampleRate, samples, timestamp } = chunkData;
        
        // 更新统计信息
        this.stats.totalChunks++;
        this.stats.totalSamples += samples;
        
        // 检测静默
        const hasSound = this.detectSound(data);
        const currentTime = Date.now() / 1000;
        
        if (hasSound) {
            this.lastSoundTime = currentTime;
            this.silenceStartTime = null;
        } else {
            if (this.silenceStartTime === null) {
                this.silenceStartTime = currentTime;
            }
            
            const silenceDuration = currentTime - this.silenceStartTime;
            if (silenceDuration >= this.silenceThreshold) {
                // 检测到静默
                if (this.onSilenceDetected) {
                    this.onSilenceDetected(silenceDuration);
                }
                this.stats.silenceDetections++;
            }
        }
        
        // 添加到队列
        if (this.audioQueue.length >= this.maxQueueSize) {
            // 队列已满，丢弃最旧的块
            this.audioQueue.shift();
            this.stats.droppedChunks++;
            console.warn('⚠️ 音频队列已满，丢弃最旧的块');
        }
        
        // 创建音频块对象
        const audioChunk = {
            data: data,
            sampleRate: sampleRate,
            samples: samples,
            timestamp: timestamp,
            hasSound: hasSound
        };
        
        this.audioQueue.push(audioChunk);
        
        // 调用回调函数
        if (this.onAudioChunk) {
            this.onAudioChunk(audioChunk);
        }
    }
    
    /**
     * 检测音频块中是否有声音
     * @param {ArrayBuffer} audioData - PCM音频数据
     * @returns {boolean} 是否有声音
     */
    detectSound(audioData) {
        // 将ArrayBuffer转换为Int16Array
        const samples = new Int16Array(audioData);
        
        // 计算RMS（均方根）值
        let sum = 0;
        for (let i = 0; i < samples.length; i++) {
            const sample = samples[i] / 32768.0; // 归一化到[-1, 1]
            sum += sample * sample;
        }
        
        const rms = Math.sqrt(sum / samples.length);
        
        // 阈值：RMS > 0.01 认为有声音
        const threshold = 0.01;
        return rms > threshold;
    }
    
    /**
     * 开始录音
     */
    async startRecording() {
        try {
            if (this.isRecording) {
                console.warn('⚠️ 已经在录音中');
                return;
            }
            
            // 检查浏览器支持
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                throw new Error('您的浏览器不支持麦克风访问功能。建议使用Chrome、Firefox、Safari等现代浏览器，并确保页面通过HTTPS访问。');
            }

            // 请求麦克风权限
            const stream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    channelCount: 1, // 单声道
                    echoCancellation: true,
                    noiseSuppression: true,
                    autoGainControl: true
                }
            });
            
            console.log('✅ 麦克风权限获取成功');
            
            // 如果AudioContext处于suspended状态，恢复它
            if (this.audioContext.state === 'suspended') {
                await this.audioContext.resume();
                console.log('✅ AudioContext已恢复');
            }
            
            // 创建MediaStreamAudioSourceNode
            this.mediaStreamSource = this.audioContext.createMediaStreamSource(stream);
            
            // 连接到AudioWorkletNode
            this.mediaStreamSource.connect(this.workletNode);
            
            // 重置状态
            this.isRecording = true;
            this.audioQueue = [];
            this.silenceStartTime = null;
            this.lastSoundTime = Date.now() / 1000;
            this.stats = {
                totalChunks: 0,
                totalSamples: 0,
                droppedChunks: 0,
                silenceDetections: 0
            };
            
            // 保存stream以便后续停止
            this.mediaStream = stream;
            
            console.log('🎤 开始录音');
            
        } catch (error) {
            console.error('❌ 开始录音失败:', error);
            if (this.onError) {
                this.onError(error);
            }
            throw error;
        }
    }
    
    /**
     * 停止录音
     */
    stopRecording() {
        if (!this.isRecording) {
            console.warn('⚠️ 当前未在录音');
            return;
        }
        
        // 断开连接
        if (this.mediaStreamSource) {
            this.mediaStreamSource.disconnect();
            this.mediaStreamSource = null;
        }
        
        // 停止媒体流
        if (this.mediaStream) {
            this.mediaStream.getTracks().forEach(track => track.stop());
            this.mediaStream = null;
        }
        
        this.isRecording = false;
        
        console.log('🛑 停止录音');
        console.log('📊 统计信息:', this.stats);
    }
    
    /**
     * 获取队列中的所有音频块
     */
    getQueuedChunks() {
        return [...this.audioQueue];
    }
    
    /**
     * 清空队列
     */
    clearQueue() {
        this.audioQueue = [];
    }
    
    /**
     * 获取统计信息
     */
    getStats() {
        return {
            ...this.stats,
            queueSize: this.audioQueue.length,
            isRecording: this.isRecording,
            silenceDuration: this.silenceStartTime 
                ? (Date.now() / 1000 - this.silenceStartTime) 
                : 0
        };
    }
    
    /**
     * 销毁资源
     */
    async destroy() {
        this.stopRecording();
        
        if (this.workletNode) {
            this.workletNode.disconnect();
            this.workletNode = null;
        }
        
        if (this.audioContext) {
            await this.audioContext.close();
            this.audioContext = null;
        }
        
        console.log('🗑️ AudioManager资源已销毁');
    }
}

// 导出到全局
if (typeof window !== 'undefined') {
    window.AudioManager = AudioManager;
}
