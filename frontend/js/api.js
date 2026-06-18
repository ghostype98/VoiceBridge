// API 基础配置
class API {
    constructor() {
        // 动态检测API服务器地址
        const protocol = window.location.protocol;
        const hostname = window.location.hostname;
        const port = window.location.port;

        // 检测是否为私有IP地址（内网）
        const isPrivateIP = (hostname) => {
            // 检查是否为私有IP范围
            const privateIPPatterns = [
                /^127\./,      // localhost
                /^192\.168\./, // 私有网络A类
                /^10\./,       // 私有网络B类
                /^172\.(1[6-9]|2[0-9]|3[0-1])\./, // 私有网络C类
                /^localhost$/,
                /^0\.0\.0\.0$/
            ];

            return privateIPPatterns.some(pattern => pattern.test(hostname));
        };

        // 检测是否为natapp域名或其他外部域名
        const isExternalDomain = (hostname) => {
            return hostname.endsWith('.natapp1.cc') ||
                   hostname.includes('.natapp.') ||
                   hostname.includes('.ngrok.') ||
                   (hostname.includes('.') && !isPrivateIP(hostname) && !/^\d+\.\d+\.\d+\.\d+$/.test(hostname));
        };

        let apiHost, apiPort;

        if (isPrivateIP(hostname)) {
            // 内网环境：使用相同的IP地址，端口与主服务一致（默认 8002，以 config/config.yaml 为准）
            apiHost = hostname;
            apiPort = port || '8002';
            console.log(`🏠 内网环境检测到，使用 ${apiHost}:${apiPort}`);
        } else if (isExternalDomain(hostname)) {
            // 外部域名环境（如natapp）：使用当前域名和协议，但不指定端口
            // 因为外部服务通常会转发所有请求到内部主服务端口
            apiHost = hostname;
            apiPort = port || (protocol === 'https:' ? '443' : '80');
            console.log(`🌐 外部域名环境检测到，使用 ${protocol}//${apiHost}:${apiPort}`);
        } else {
            // 其他情况：尝试使用当前主机
            apiHost = hostname;
            apiPort = port || '8002';
            console.log(`❓ 未知环境，使用 ${apiHost}:${apiPort}`);
        }

        // 构造baseURL
        this.baseURL = `${protocol}//${apiHost}`;
        if ((protocol === 'http:' && apiPort !== '80') ||
            (protocol === 'https:' && apiPort !== '443')) {
            this.baseURL += `:${apiPort}`;
        }
        this.baseURL += '/api/v1';

        console.log('🔗 API baseURL 设置为:', this.baseURL);
    }

    // 通用请求方法
    async request(endpoint, options = {}) {
        const url = `${this.baseURL}${endpoint}`;
        
        // 如果body是FormData，不设置Content-Type，让浏览器自动设置
        const isFormData = options.body instanceof FormData;
        const config = {
            headers: isFormData ? {
                ...options.headers
            } : {
                'Content-Type': 'application/json',
                ...options.headers
            },
            ...options
        };


        try {
            console.log('🌐 API请求:', url);
            const response = await fetch(url, config);

            const contentType = response.headers.get('content-type');
            
            if (contentType && contentType.includes('application/json')) {
                const data = await response.json();
                
                if (!response.ok) {
                    throw new Error(data.detail || `HTTP ${response.status}`);
                }
                
                return data;
            } else {
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                return response;
            }
        } catch (error) {
            console.error('API请求错误:', error);

            // 如果是连接错误，尝试备用URL
            if (error.message.includes('Failed to fetch') || error.message.includes('NetworkError')) {
                console.log('🔄 检测到网络错误，尝试备用API地址...');

                // 尝试localhost作为备用
                const backupUrl = url.replace(/:\/\/[^\/]+/, '://localhost:8002');
                if (backupUrl !== url) {
                    console.log('🔄 尝试备用URL:', backupUrl);
                    try {
                        const backupResponse = await fetch(backupUrl, config);
                        const backupContentType = backupResponse.headers.get('content-type');

                        if (backupContentType && backupContentType.includes('application/json')) {
                            const backupData = await backupResponse.json();

                            if (!backupResponse.ok) {
                                throw new Error(backupData.detail || `HTTP ${backupResponse.status}`);
                            }

                            console.log('✅ 备用API地址成功');
                            return backupData;
                        } else {
                            if (!backupResponse.ok) {
                                throw new Error(`HTTP ${backupResponse.status}`);
                            }
                            return backupResponse;
                        }
                    } catch (backupError) {
                        console.error('❌ 备用API地址也失败:', backupError);
                    }
                }
            }

            throw error;
        }
    }

    // GET 请求
    async get(endpoint) {
        return this.request(endpoint, { method: 'GET' });
    }

    // POST 请求
    async post(endpoint, data) {
        return this.request(endpoint, {
            method: 'POST',
            body: JSON.stringify(data)
        });
    }

    // PUT 请求
    async put(endpoint, data) {
        return this.request(endpoint, {
            method: 'PUT',
            body: JSON.stringify(data)
        });
    }

    // DELETE 请求
    async delete(endpoint) {
        return this.request(endpoint, { method: 'DELETE' });
    }
}



// 认证相关 API
class AuthAPI extends API {
    // 用户登录
    async login(username, password) {
        return this.post('/auth/login', {
            username: username,
            password: password
        });
    }

    // 检查会话状态
    async checkSession(invitationId) {
        return this.post('/auth/check-session', {
            invitation_id: invitationId
        });
    }
}


// 面试会话相关 API
class InterviewSessionAPI extends API {
    // 创建面试会话
    async createSession(invitationId) {
        return this.post('/interview-sessions', {
            invitation_id: invitationId
        });
    }

    // 获取会话信息
    async getSession(sessionId) {
        return this.get(`/interview-sessions/${sessionId}`);
    }

    // 已取消：获取用户所有会话的API
    // async getUserSessions() {
    //     return this.get('/interview-sessions');
    // }

    // 开始会话
    async startSession(sessionId) {
        return this.post(`/interview-sessions/${sessionId}/start`, {});
    }

    // 暂停会话
    async pauseSession(sessionId) {
        return this.post(`/interview-sessions/${sessionId}/pause`, {});
    }

    // 恢复会话
    async resumeSession(sessionId) {
        return this.post(`/interview-sessions/${sessionId}/resume`, {});
    }

    // 完成会话
    async completeSession(sessionId) {
        return this.post(`/interview-sessions/${sessionId}/complete`, {});
    }

    // 取消会话
    async cancelSession(sessionId) {
        return this.post(`/interview-sessions/${sessionId}/cancel`, {});
    }

    // 添加回答
    async addAnswer(sessionId, answerData) {
        return this.post(`/interview-sessions/${sessionId}/answers`, answerData);
    }

    // 获取对话历史
    async getConversationHistory(sessionId) {
        return this.get(`/interview-sessions/${sessionId}/history`);
    }

    // 获取会话统计
    async getSessionStats(sessionId) {
        return this.get(`/interview-sessions/${sessionId}/stats`);
    }
}

// 面试流程相关 API
class InterviewFlowAPI extends API {
    // 面试页 UI 开关（如是否显示实时转写）
    async getInterviewUiConfig() {
        return this.get('/interview/ui-config');
    }

    // 开始面试
    async startInterview(invitationId) {
        return this.post('/interview/start', {
            session_id: invitationId  // 这里传递的是invitation_id，后端会处理
        });
    }

    // 初始化TTS服务
    async initializeTTS() {
        return this.post('/tts/initialize', {});
    }

    // TTS文字转语音
    async synthesizeSpeech(text) {
        return this.post('/api/v1/tts/synthesize', {
            text: text,
            voice_type: 'female'  // 可以根据需要调整
        });
    }

    // 初始化ASR服务
    async initializeASR() {
        return this.post('/asr/initialize', {});
    }

    // 获取下一问题
    async getNextQuestion(sessionId) {
        return this.get(`/interview-flow/next-question?session_id=${sessionId}`);
    }

    // 提交语音回答
    async submitVoiceAnswer(sessionId, audioBase64) {
        return this.post('/interview-flow/voice-answer', {
            session_id: sessionId,
            audio_base64: audioBase64
        });
    }

    // 提交文本回答
    async submitTextAnswer(sessionId, questionId, answerText) {
        return this.post('/api/v1/interview-flow/text-answer', {
            session_id: sessionId,
            question_id: questionId,
            answer_text: answerText
        });
    }

    // 暂停面试
    async pauseInterview(sessionId) {
        return this.post(`/interview-flow/pause?session_id=${sessionId}`, {});
    }

    // 恢复面试
    async resumeInterview(sessionId) {
        return this.post(`/interview-flow/resume?session_id=${sessionId}`, {});
    }

    // 完成面试
    async completeInterview(sessionId) {
        // 后端期望Form数据，使用FormData
        const formData = new FormData();
        formData.append('session_id', sessionId);
        
        return this.request(`/interview/complete`, {
            method: 'POST',
            body: formData
        });
    }

    // 获取当前问题（interview_session 路由）。推荐传入 invitationId，后端会按邀请维度的会话推算当前题，避免 session_id 与 DB 不一致导致 404
    async getCurrentQuestion(sessionId, invitationId = null) {
        const base = `/interview-sessions/${sessionId}/current-question`;
        const url = invitationId ? `${base}?invitation_id=${encodeURIComponent(invitationId)}` : base;
        return this.get(url);
    }

    // 播放问题音频
    async playQuestionAudio(sessionId) {
        return this.get(`/interview-flow/play-audio?session_id=${sessionId}`);
    }

    // 获取基本题目列表
    async getBasicQuestions(invitationId) {
        return this.get(`/interview-sessions/${invitationId}/basic-questions`);
    }

    // 获取专业题目列表
    async getProfessionalQuestions(invitationId) {
        return this.get(`/interview-sessions/${invitationId}/professional-questions`);
    }

    // 获取完整的面试题目列表（包含题目文本和所有信息）
    async getAllQuestions(invitationId) {
        return this.get(`/voice-interview/interview/${invitationId}/questions`);
    }
}

// 创建 API 实例
const authAPI = new AuthAPI();
const sessionAPI = new InterviewSessionAPI();
const interviewAPI = new InterviewFlowAPI();

// 导出全局变量
window.API = {
    auth: authAPI,
    session: sessionAPI,
    interview: interviewAPI
};