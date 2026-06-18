/**
 * WebSocket ASR 客户端
 * 与后端语音流式服务保持一致的协议：
 *   1. 建立 WebSocket 连接
 *   2. 接收 connection_established 后发送 start_recording
 *   3. 发送原始 PCM 数据（二进制帧）
 *   4. 处理 intermediate_text / final_text / evaluation_result 等消息
 */

class WebSocketASRClient {
    constructor(options = {}) {
        this.endpoint = options.endpoint || '/ws/asr';
        this.onTranscript = options.onTranscript || null;
        this.onEvaluation = options.onEvaluation || null;
        this.onConnect = options.onConnect || null;
        this.onDisconnect = options.onDisconnect || null;
        this.onError = options.onError || null;
        this.onFollowUpTrigger = options.onFollowUpTrigger || null;
        this.onFollowUpPending = options.onFollowUpPending || null;
        this.onQuestionSwitch = options.onQuestionSwitch || null;
        this.onNextQuestion = options.onNextQuestion || null;
        this.onInterviewCompleted = options.onInterviewCompleted || null;
        this.onSilenceCountdown = options.onSilenceCountdown || null;
        this.onRecordingStopped = options.onRecordingStopped || null;

        this.ws = null;
        this.isConnected = false;
        this.started = false;
        this.connectionId = null;

        this.invitationId = null;
        this.questionId = null;
        this.sessionId = null;

        this._pendingConnect = null;
        /** 已发出 start_recording，尚未收到 recording_started，用于去重 */
        this._awaitingRecordingStarted = false;
    }

    buildWebSocketURL() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const hostname = window.location.hostname;
        const port = window.location.port || (protocol === 'wss:' ? '443' : '80');
        // 默认端口 80/443 不写端口号，避免经 natapp 等代理时无法正确转发
        const portSuffix = (protocol === 'wss:' && port === '443') || (protocol === 'ws:' && port === '80') ? '' : `:${port}`;
        return `${protocol}//${hostname}${portSuffix}${this.endpoint}`;
    }

    async connect({ invitationId, questionId, sessionId }) {
        const prevQuestionId = this.questionId;
        this.invitationId = invitationId;
        this.sessionId = sessionId;

        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            if (this.started) {
                if (questionId && questionId !== prevQuestionId) {
                    this.switchQuestion(questionId);
                } else if (questionId) {
                    this.questionId = questionId;
                }
                return;
            }
            this.questionId = questionId;
            this._startRecordingSession();
            return;
        }

        this.questionId = questionId;

        return new Promise((resolve, reject) => {
            this._pendingConnect = { resolve, reject };

            try {
                const url = this.buildWebSocketURL();
                console.log(`🔗 连接语音流式WebSocket: ${url}`);
                this.ws = new WebSocket(url);
                this.ws.binaryType = 'arraybuffer';
                this.ws.onopen = () => {
                    console.log('✅ WebSocket 已打开');
                };
                this.ws.onmessage = (event) => this._handleMessage(event);
                this.ws.onerror = (event) => this._handleError(event);
                this.ws.onclose = (event) => this._handleClose(event);
            } catch (error) {
                this._handleError(error);
                reject(error);
            }
        });
    }

    _handleMessage(event) {
        if (!(typeof event.data === 'string')) {
            console.debug('收到二进制消息（忽略）', event.data);
            return;
        }

        let message;
        try {
            message = JSON.parse(event.data);
        } catch (error) {
            console.error('解析WebSocket消息失败:', error);
            return;
        }

        const { type } = message;
        switch (type) {
            case 'connection_established':
                this.connectionId = message.connection_id;
                this.isConnected = true;
                this.started = false;
                this._awaitingRecordingStarted = false;
                this._startRecordingSession();
                if (this._pendingConnect) {
                    this._pendingConnect.resolve();
                    this._pendingConnect = null;
                }
                if (this.onConnect) {
                    this.onConnect(message);
                }
                break;
            case 'intermediate_text':
                if (this.onTranscript) {
                    this.onTranscript({
                        type: 'intermediate',
                        text: message.text || '',
                        questionId: message.question_id
                    });
                }
                break;
            case 'final_text': {
                const text = message.current_question_text || message.accumulated_text || message.text || '';
                if (this.onTranscript) {
                    this.onTranscript({
                        type: 'final',
                        text,
                        questionId: message.question_id
                    });
                }
                break;
            }
            case 'evaluation_result':
                if (this.onEvaluation) {
                    this.onEvaluation(message.result);
                }
                break;
            case 'error':
                this._awaitingRecordingStarted = false;
                this._handleError(new Error(message.message || '语音流服务错误'));
                break;
            case 'recording_started':
                this.started = true;
                this._awaitingRecordingStarted = false;
                console.log('✅ 录音已开始，可以发送音频数据');
                break;
            case 'recording_stopped':
                this.started = false;
                this._awaitingRecordingStarted = false;
                if (this.onRecordingStopped) {
                    try {
                        this.onRecordingStopped(message);
                    } catch (e) {
                        console.error('onRecordingStopped 回调异常:', e);
                    }
                }
                break;
            case 'follow_up_trigger':
                // 追问触发
                if (this.onFollowUpTrigger) {
                    this.onFollowUpTrigger({
                        question: message.question,
                        question_for_tts: message.question_for_tts || message.question,
                        question_role: message.question_role || 'follow_up',
                        is_follow_up: message.is_follow_up || true,
                        follow_up_question_id: message.follow_up_question_id,
                        parent_answer_id: message.parent_answer_id,
                        reason: message.reason
                    });
                }
                break;
            case 'follow_up_pending':
                if (this.onFollowUpPending) {
                    try {
                        this.onFollowUpPending(message);
                    } catch (e) {
                        console.error('onFollowUpPending 回调异常:', e);
                    }
                } else {
                    console.warn('追问未完成，暂不能切题:', message.message || message);
                }
                break;
            case 'question_switched':
                // 题目切换确认（后端字段为 question_id / old_question_id）
                if (this.onQuestionSwitch) {
                    this.onQuestionSwitch({
                        oldQuestionId: message.old_question_id,
                        newQuestionId: message.question_id || message.new_question_id
                    });
                }
                break;
            case 'next_question':
                // 下一题通知
                if (this.onNextQuestion) {
                    this.onNextQuestion({
                        currentQuestionId: message.current_question_id,
                        nextQuestionId: message.next_question_id,
                        autoAdvanced: message.auto_advanced
                    });
                }
                break;
            case 'interview_completed':
                // 面试完成通知
                if (this.onInterviewCompleted) {
                    this.onInterviewCompleted({
                        durationMinutes: message.duration_minutes || null
                    });
                }
                break;
            case 'silence_countdown':
                // 静音倒计时
                if (this.onSilenceCountdown) {
                    this.onSilenceCountdown({
                        countdown: message.countdown || 3
                    });
                }
                break;
            case 'silence_cancelled':
                // 静音倒计时已取消
                console.log('静音倒计时已取消');
                break;
            case 'pong':
                // 心跳响应
                break;
            default:
                console.debug('未处理的WebSocket消息:', type, message);
        }
    }

    _handleError(error) {
        console.error('WebSocket ASR 错误:', error);
        if (this.onError) {
            this.onError(error);
        }
        if (this._pendingConnect) {
            this._pendingConnect.reject(error);
            this._pendingConnect = null;
        }
    }

    _handleClose(event) {
        this.isConnected = false;
        this.started = false;
        this._awaitingRecordingStarted = false;
        if (this.onDisconnect) {
            this.onDisconnect(event);
        }
        if (this._pendingConnect) {
            this._pendingConnect.reject(new Error('WebSocket连接已关闭'));
            this._pendingConnect = null;
        }
    }

    _startRecordingSession() {
        if (!this.isConnected || this.started || this._awaitingRecordingStarted) {
            return;
        }
        const payload = {
            invitation_id: this.invitationId,
            question_id: this.questionId,
            session_id: this.sessionId,
            timestamp: Date.now()
        };
        this._awaitingRecordingStarted = true;
        this.sendControlMessage('start_recording', payload);
    }

    sendControlMessage(type, payload = {}) {
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
            console.warn('WebSocket 未连接，无法发送控制消息');
            return false;
        }
        try {
            const message = { type, ...payload };
            this.ws.send(JSON.stringify(message));
            console.log('📤 发送控制消息:', type, payload);
            return true;
        } catch (error) {
            this._handleError(error);
            return false;
        }
    }

    sendAudioChunk(chunk) {
        if (!this.started || !this.ws || this.ws.readyState !== WebSocket.OPEN) {
            console.warn('WebSocket 未准备好或录音未开始，音频块未发送');
            return false;
        }

        const buffer = chunk && chunk.data ? chunk.data : chunk;
        if (!buffer) {
            console.warn('无效的音频块，忽略发送');
            return false;
        }

        try {
            this.ws.send(buffer);
            return true;
        } catch (error) {
            this._handleError(error);
            return false;
        }
    }

    switchQuestion(questionId) {
        if (!questionId) return false;
        if (questionId === this.questionId) {
            console.debug('switchQuestion: 题目未变化，跳过发送 switch_question');
            return true;
        }
        this.questionId = questionId;
        return this.sendControlMessage('switch_question', {
            invitation_id: this.invitationId,
            question_id: questionId,
            timestamp: Date.now()
        });
    }

    stopRecording() {
        if (!this.started) {
            return false;
        }
        this.started = false;
        return this.sendControlMessage('stop_recording', {
            timestamp: Date.now()
        });
    }

    /** 告知后端 TTS 是否在播报题干，用于抑制 ASR 误触「切题」类关键词 */
    notifyTtsPlaybackState(playing) {
        return this.sendControlMessage('tts_playback_state', {
            playing: !!playing,
            timestamp: Date.now()
        });
    }

    disconnect() {
        if (this.ws) {
            this.ws.close(1000, '客户端主动断开');
            this.ws = null;
        }
        this.isConnected = false;
        this.started = false;
        this._awaitingRecordingStarted = false;
        this.connectionId = null;
    }
}

if (typeof window !== 'undefined') {
    window.WebSocketASRClient = WebSocketASRClient;
}
