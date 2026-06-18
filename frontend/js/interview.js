// 面试页面主要功能
console.log('🔧 interview.js 开始加载...');

// 抑制 RecordRTC 的 ScriptProcessorNode 弃用警告（这是第三方库的问题，不影响功能）
(function() {
    const originalWarn = console.warn;
    console.warn = function(...args) {
        // 过滤掉 RecordRTC 的 ScriptProcessorNode 弃用警告
        const message = args.join(' ');
        if (message.includes('ScriptProcessorNode is deprecated') || 
            message.includes('Use AudioWorkletNode instead')) {
            // 这是 RecordRTC 库内部使用的已弃用 API，不影响功能，静默忽略
            return;
        }
        // 其他警告正常显示
        originalWarn.apply(console, args);
    };
})();

class InterviewManager {
    constructor() {
        this.currentUser = null;
        this.currentSession = null;
        this.currentQuestion = null;
        this.currentInvitationId = null;
        this.mediaRecorder = null;
        this.audioChunks = [];
        this.stream = null;
        this.recordRTC = null; // RecordRTC实例（参考旧代码）
        this.recordingTimer = null;
        this.recordingDuration = 0;

        // 实时语音转录相关
        this.transcriptBuffer = '';
        this.intermediateText = ''; // 临时转写文本（正在识别中，实时显示）
        this.transcribedText = ''; // 最终转写文本（已确认的转写结果）
        this.accumulatedText = ''; // 累积文本
        this.lastAudioTime = 0;
        this.silenceTimeout = null;
        this.isRealTimeMode = false; // 标记是否处于实时转录模式
        
        // 面试倒计时（单位：秒，默认40分钟 = 2400秒；登录后会根据邀请配置覆盖）
        this.INTERVIEW_DURATION = 40 * 60;
        this.interviewRemainingTime = this.INTERVIEW_DURATION; // 面试剩余时间（会在加载invitation_data后更新）
        this.interviewTimer = null;
        
        // 切换状态锁（参考旧代码）
        this.isSwitching = false;

        // 追问状态标记（防止追问消息被清空）
        this.isFollowUpActive = false;
        this.followUpTimeout = null;

        // 双缓存机制
        this.fullRecordingChunks = []; // 全程录音缓存
        this.currentQuestionChunks = []; // 当前题目音频缓存
        this.fullRecordingRecorder = null; // 全程录音的MediaRecorder
        this.currentQuestionRecorder = null; // 当前题目录音的MediaRecorder

        // 静音检测相关
        this.silenceThreshold = -40; // 静音阈值（分贝），可根据环境调整
        this.silenceDuration = 0; // 当前静音持续时间
        this.lastSoundTime = 0; // 最后检测到声音的时间
        this.audioContext = null; // Web Audio API上下文
        this.analyser = null; // 音频分析器
        this.volumeCheckInterval = null; // 音量检测定时器

        // 题目列表
        this.basicQuestions = [];
        this.professionalQuestions = [];
        this.allQuestions = []; // 合并的题目列表，包含所有question_id
        this.currentQuestionIndex = 0; // 当前题目在allQuestions中的索引
        this.totalQuestions = 0;

        // 事件绑定状态
        this.eventsBound = false;

        // 初始化状态标志
        this.pageInitialized = false;
        this.interviewStarted = false;

        // 新的音频系统（Web Audio API + AudioWorklets）
        this.audioManager = null;
        this.wsASRClient = null;
        this.useNewAudioSystem = false; // 标志：是否使用新的音频系统（改为false，使用RecordRTC方式）

        // RecordRTC 相关缓冲（前端发送队列，平滑 WebSocket 流）
        this.recordRTCAudioQueue = [];
        this.recordRTCSendTimer = null;

        // 是否显示实时转写（服务端 interview_ui.show_asr_text；false 时仅视觉隐藏，ASR 逻辑不变）
        this.showAsrText = true;
        this._volumeVisualizerRaf = null;
        this._volumeVisualizerRunning = false;
        this._volumeBarSmooth = null;

        /** 等待服务端 WebSocket `recording_stopped` 时的一次性 resolve（由 wsASRClient.onRecordingStopped 触发） */
        this._recordingStoppedResolver = null;

        // 暂时屏蔽题目语音存储功能

        this.initializePage();
    }

    // 检查浏览器兼容性（参考voice_interview_streaming模式）
    checkBrowserCompatibility() {
        const result = {
            supported: true,
            message: '',
            level: 'full' // full, partial, none
        };

        // 检查MediaDevices API（更宽松的检查）
        // 注意：某些浏览器可能没有 navigator.mediaDevices，但有 navigator.getUserMedia（旧API）
        const hasMediaDevices = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
        const hasLegacyGetUserMedia = !!(navigator.getUserMedia || navigator.webkitGetUserMedia || navigator.mozGetUserMedia);
        
        if (!hasMediaDevices && !hasLegacyGetUserMedia) {
            result.supported = false;
            result.message = '您的浏览器不支持麦克风访问功能。建议使用Chrome 47+、Firefox 44+、Safari 14+等现代浏览器。';
            result.level = 'none';
            return result;
        }

        // 如果只有旧API，标记为部分支持
        if (!hasMediaDevices && hasLegacyGetUserMedia) {
            console.warn('⚠️ 浏览器使用旧版getUserMedia API，建议升级浏览器');
            result.level = 'partial';
        }

        // 检查Web Audio API（用于音频处理）
        if (!window.AudioContext && !window.webkitAudioContext) {
            console.warn('⚠️ 不支持Web Audio API，将使用基础录音功能');
            result.level = 'partial';
        }

        // 检查MediaRecorder（用于录音）
        if (!window.MediaRecorder) {
            console.warn('⚠️ 不支持MediaRecorder，将使用备用录音方案');
            result.level = 'partial';
        }

        // 检查WebSocket（用于实时通信）
        if (!window.WebSocket) {
            console.warn('⚠️ 不支持WebSocket，实时功能将受限');
            result.level = 'partial';
        }

        return result;
    }

    // 获取最佳音频约束（参考voice_interview_streaming模式）
    getOptimalAudioConstraints() {
        const isMobile = /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);

        // 基础音频约束
        const baseConstraints = {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true
        };

        // 移动设备优化
        if (isMobile) {
            return {
                ...baseConstraints,
                sampleRate: 16000, // 降低采样率节省带宽
                channelCount: 1,    // 单声道
                // 移动设备可能不支持高级约束，使用基础设置
            };
        }

        // 桌面设备：尝试使用更高质量的设置
        try {
            // 检查是否支持getSupportedConstraints
            if (navigator.mediaDevices && navigator.mediaDevices.getSupportedConstraints) {
                const supported = navigator.mediaDevices.getSupportedConstraints();
                console.log('🎛️ 浏览器支持的音频约束:', supported);

                // 如果支持高级约束，添加更多优化
                if (supported.sampleRate && supported.channelCount) {
                    return {
                        ...baseConstraints,
                        sampleRate: 44100,    // 高质量采样率
                        channelCount: 1,      // 单声道
                        latency: 0.01,        // 低延迟（如果支持）
                        volume: 1.0          // 音量（如果支持）
                    };
                }
            }
        } catch (error) {
            console.warn('⚠️ 获取支持的约束失败，使用基础约束:', error);
        }

        // 回退到基础约束
        return baseConstraints;
    }

    // 验证音频流质量
    validateAudioStream(stream) {
        try {
            const audioTrack = stream.getAudioTracks()[0];
            if (audioTrack) {
                const settings = audioTrack.getSettings();
                console.log('🎵 音频流设置:', {
                    sampleRate: settings.sampleRate,
                    channelCount: settings.channelCount,
                    latency: settings.latency,
                    volume: settings.volume
                });

                // 检查音频质量
                if (settings.sampleRate && settings.sampleRate < 8000) {
                    console.warn('⚠️ 音频采样率较低，可能影响识别质量');
                }
                if (settings.channelCount && settings.channelCount > 1) {
                    console.log('ℹ️ 检测到立体声输入，将转换为单声道处理');
                }
            }
        } catch (error) {
            console.warn('⚠️ 无法验证音频流质量:', error);
        }
    }

    // 使用回退约束申请麦克风权限（针对OverconstrainedError）
    async requestMicrophonePermissionWithFallback() {
        try {
            console.log('🔄 使用基础音频约束重试权限申请...');

            // 使用最基础的约束
            const fallbackConstraints = {
                audio: {
                    echoCancellation: false,
                    noiseSuppression: false,
                    autoGainControl: false
                }
            };

            const stream = await navigator.mediaDevices.getUserMedia(fallbackConstraints);

            console.log('✅ 使用基础约束成功获取麦克风权限');
            this.showPermissionStatus('已授予', '麦克风权限已获取（使用基础音频设置）');

            // 验证并停止流
            this.validateAudioStream(stream);
            stream.getTracks().forEach(track => track.stop());

        } catch (fallbackError) {
            console.error('❌ 基础约束也失败:', fallbackError);
            this.showPermissionStatus('失败', '无法获取麦克风权限，请检查设备和浏览器设置。');
        }
    }

    // 初始化页面
    async initializePage() {
        if (this.pageInitialized) {
            console.log('⚠️ 页面已经初始化，跳过重复初始化');
            return;
        }
        this.pageInitialized = true;

        try {
            console.log('🚀 开始页面初始化流程');
            // 第一步：检查认证状态（如果失败会直接重定向）
            const authResult = await this.checkAuthStatus();
            if (!authResult) {
                // 认证失败，已重定向，不继续初始化
                return;
            }

            // 第二步：初始化用户状态
            await this.initializeUser();

            await this.loadInterviewUiConfig();

            // 第三步：初始化UI
            this.initializeUI();

            // 第四步：绑定事件监听器
            this.bindEventListeners();

            // 第五步：加载用户信息
            await this.loadUserInfo();

            // 第六步：自动开始面试
            console.log('🎯 用户认证成功，自动开始面试...');
            await this.startInterview();

            // 第七步：检查麦克风权限
            // 统一检查权限，不区分移动设备和桌面设备
            await this.checkMicrophonePermission();

            console.log('页面初始化完成');

        } catch (error) {
            console.error('页面初始化失败:', error);
            // 更详细的错误信息
            const errorMsg = error.message || '页面初始化失败，请刷新重试';
            this.showError(errorMsg);
        }
    }

    // 检查认证状态 - 在页面加载早期立即检查
    async checkAuthStatus() {
        try {
            console.log('检查用户认证状态...');

            const isLoggedIn = localStorage.getItem('isLoggedIn');
            const userData = localStorage.getItem('userData');
            const invitationData = localStorage.getItem('invitationData');

            // 严格检查认证状态
            if (!isLoggedIn || isLoggedIn !== 'true') {
                console.warn('用户未登录，重定向到登录页面');
                this.redirectToLogin('用户未登录，请先登录');
                return false;
            }

            if (!userData) {
                console.warn('用户信息不存在，重定向到登录页面');
                this.redirectToLogin('用户信息不完整，请重新登录');
                return false;
            }

            // 验证用户数据格式
            let parsedUserData;
            try {
                parsedUserData = JSON.parse(userData);
            } catch (e) {
                console.error('用户信息格式错误:', e);
                this.redirectToLogin('用户信息格式错误，请重新登录');
                return false;
            }

            // 检查必要字段
            if (!parsedUserData.invitation_data || !parsedUserData.invitation_data.invitation_id) {
                console.warn('用户信息不完整，重定向到登录页面');
                this.redirectToLogin('用户信息不完整，请重新登录');
                return false;
            }

            // 调用后端API验证会话
            try {
                const sessionCheckResult = await this.checkSessionWithBackend(parsedUserData.invitation_data.invitation_id);
                if (!sessionCheckResult.valid) {
                    console.warn('后端会话验证失败:', sessionCheckResult.message);
                    this.redirectToLogin(sessionCheckResult.message || '会话已过期，请重新登录');
                    return false;
                }

                console.log('后端会话验证通过');
            } catch (error) {
                console.error('后端会话验证出错:', error);
                // 如果后端验证失败，为了用户体验，仍然允许继续，但记录警告
                console.warn('后端验证失败，继续使用本地认证');
            }

            // 恢复用户数据
            this.currentUser = parsedUserData;

            // 如果有邀请数据，合并到用户对象
            if (invitationData) {
                try {
                    const parsedInvitationData = JSON.parse(invitationData);
                    this.currentUser.invitation_data = parsedInvitationData;
                } catch (e) {
                    console.warn('邀请数据解析失败:', e);
                    // 不致命，继续处理
                }
            }

            console.log('用户认证通过:', this.currentUser.username);
            return true;

        } catch (error) {
            console.error('检查认证状态失败:', error);
            this.redirectToLogin('认证检查失败，请重新登录');
            return false;
        }
    }

    // 重定向到登录页面
    redirectToLogin(reason) {
        console.log(`重定向原因: ${reason}`);
        // 清除可能存在的无效认证数据
        localStorage.removeItem('isLoggedIn');
        localStorage.removeItem('userData');
        localStorage.removeItem('invitationData');
        // 重定向到登录页面
        window.location.href = '/login';
    }

    // 调用后端API验证会话
    async checkSessionWithBackend(invitationId) {
        try {
            const response = await fetch('/api/v1/auth/check-session', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    invitation_id: invitationId
                })
            });

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }

            const result = await response.json();
            return result;

        } catch (error) {
            console.error('后端会话验证请求失败:', error);
            // 如果网络错误，返回无效状态
            return {
                valid: false,
                message: '网络连接失败，无法验证会话'
            };
        }
    }

    // 初始化用户状态
    async initializeUser() {
        // 用户数据已经在checkAuthStatus中设置，这里不需要额外操作
        // 如果没有用户数据，checkAuthStatus会跳转到登录页面

        // 暂时屏蔽TTS服务状态检查
        console.log('TTS相关初始化和服务检查已暂时屏蔽');
    }

    // 检查TTS服务状态 - 暂时屏蔽
    async checkTTSServiceStatus() {
        // 暂时屏蔽TTS功能
        this.ttsServiceAvailable = false;
        console.log('TTS功能已暂时屏蔽，只使用文本模式');
    }

    // 检查麦克风权限（基于voice_interview_streaming参考模式）
    async checkMicrophonePermission() {
        try {
            console.log('🔍 开始检查麦克风权限...');

            // 1. 检查浏览器兼容性
            const compatibilityCheck = this.checkBrowserCompatibility();
            if (!compatibilityCheck.supported) {
                this.showPermissionStatus('不支持', compatibilityCheck.message);
                return;
            }

            // 2. 检查是否在安全上下文中（更宽松的检查）
            // localhost、127.0.0.1 和内网地址被视为安全上下文，即使使用 HTTP
            const isLocalhost = window.location.hostname === 'localhost' || 
                               window.location.hostname === '127.0.0.1';
            const isPrivateNetwork = window.location.hostname.startsWith('192.168.') ||
                                    window.location.hostname.startsWith('10.') ||
                                    window.location.hostname.startsWith('172.');
            
            // 如果是内网 IP 且不在安全上下文中，Chrome 会阻止访问麦克风
            if (!window.isSecureContext && isPrivateNetwork && !isLocalhost) {
                console.warn('⚠️ 检测到内网 IP 地址，Chrome 可能阻止在 HTTP 下访问麦克风');
                console.warn('💡 建议：使用 http://localhost:' + (window.location.port || '8002') + window.location.pathname + ' 访问');
                // 不直接返回，允许尝试（某些浏览器配置可能允许）
            }
            
            if (!window.isSecureContext && !isLocalhost && !isPrivateNetwork) {
                console.warn('⚠️ 不在安全上下文中，建议使用HTTPS或内网地址');
                // 不直接返回，允许尝试访问麦克风（某些浏览器可能仍然支持）
            }

            // 3. 检查权限API支持情况（可选，不支持时直接尝试申请权限）
            if (!navigator.permissions) {
                console.warn('⚠️ 浏览器不支持权限API，将在首次使用时申请权限');
                // 不返回，直接尝试申请权限
                await this.requestMicrophonePermission();
                return;
            }

            // 4. 查询麦克风权限状态
            console.log('📋 查询麦克风权限状态...');
            const permissionStatus = await navigator.permissions.query({ name: 'microphone' });
            console.log('麦克风权限状态:', permissionStatus.state);

            switch (permissionStatus.state) {
                case 'granted':
                    console.log('麦克风权限已授予');
                    this.showPermissionStatus('已授予', '麦克风权限正常，您可以开始语音输入');
                    break;

                case 'denied':
                    console.warn('麦克风权限已被拒绝');
                    this.showPermissionStatus('已拒绝', '麦克风权限已被拒绝，请在浏览器设置中允许访问麦克风');
                    break;

                case 'prompt':
                    console.log('麦克风权限待用户确认，尝试申请权限...');
                    await this.requestMicrophonePermission();
                    break;

                default:
                    console.log('麦克风权限状态未知:', permissionStatus.state);
                    this.showPermissionStatus('未知', '无法确定麦克风权限状态');
            }

            // 监听权限变化
            permissionStatus.addEventListener('change', () => {
                console.log('麦克风权限状态发生变化:', permissionStatus.state);
                this.handlePermissionChange(permissionStatus.state);
            });

        } catch (error) {
            console.error('检查麦克风权限失败:', error);
            this.showPermissionStatus('检查失败', `权限检查失败: ${error.message}`);
        }
    }

    // 申请麦克风权限（参考voice_interview_streaming模式优化）
    async requestMicrophonePermission() {
        try {
            console.log('🎙️ 正在申请麦克风权限...');
            this.showPermissionStatus('申请中', '正在请求麦克风权限，请在弹窗中点击"允许"...');

            // 1. 最终兼容性检查
            const compatibilityCheck = this.checkBrowserCompatibility();
            if (!compatibilityCheck.supported) {
                throw new Error(compatibilityCheck.message);
            }

            // 2. 检查是否在安全上下文中（更宽松的检查）
            // localhost、127.0.0.1 和内网地址被视为安全上下文，即使使用 HTTP
            const isLocalhost = window.location.hostname === 'localhost' || 
                               window.location.hostname === '127.0.0.1';
            const isPrivateNetwork = window.location.hostname.startsWith('192.168.') ||
                                    window.location.hostname.startsWith('10.') ||
                                    window.location.hostname.startsWith('172.');
            
            if (!window.isSecureContext && !isLocalhost && !isPrivateNetwork) {
                console.warn('⚠️ 不在安全上下文中，但继续尝试申请权限');
                // 不抛出错误，允许尝试（某些浏览器可能仍然支持）
            }

            // 3. 根据浏览器能力选择最佳音频约束
            const audioConstraints = this.getOptimalAudioConstraints();

            console.log('🎛️ 使用音频约束:', audioConstraints);

            // 4. 尝试获取音频流（这会触发权限请求）
            // 对于内网 IP 地址，Chrome 可能不允许在 HTTP 下访问麦克风
            // 尝试使用 mediaDevices API，如果失败则尝试旧版 API
            let stream;
            try {
                if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
                    stream = await navigator.mediaDevices.getUserMedia({
                        audio: audioConstraints,
                        video: false
                    });
                } else if (navigator.getUserMedia) {
                    // 回退到旧版 API（Promise 包装）
                    stream = await new Promise((resolve, reject) => {
                        navigator.getUserMedia(
                            { audio: audioConstraints, video: false },
                            resolve,
                            reject
                        );
                    });
                } else if (navigator.webkitGetUserMedia) {
                    // WebKit 旧版 API
                    stream = await new Promise((resolve, reject) => {
                        navigator.webkitGetUserMedia(
                            { audio: audioConstraints, video: false },
                            resolve,
                            reject
                        );
                    });
                } else {
                    throw new Error('浏览器不支持麦克风访问 API');
                }
            } catch (getUserMediaError) {
                // 如果是安全上下文错误，提供解决方案
                if (getUserMediaError.name === 'NotAllowedError' || 
                    getUserMediaError.name === 'NotReadableError' ||
                    getUserMediaError.message.includes('secure context')) {
                    const isPrivateNetwork = window.location.hostname.startsWith('192.168.') ||
                                            window.location.hostname.startsWith('10.') ||
                                            window.location.hostname.startsWith('172.');
                    
                    if (isPrivateNetwork && !window.isSecureContext) {
                        const solutionMsg = `
Chrome 浏览器安全策略限制：内网 IP 地址在 HTTP 下无法访问麦克风。

解决方案（任选其一）：
1. 使用 HTTPS（推荐生产环境）
2. 使用 localhost 访问：http://localhost:${window.location.port || '8002'}
3. Chrome 设置：访问 chrome://flags/#unsafely-treat-insecure-origin-as-secure
   添加地址：http://${window.location.hostname}:${window.location.port || '8002'}
   然后重启 Chrome
4. 命令行启动 Chrome（开发环境）：
   chrome --unsafely-treat-insecure-origin-as-secure=http://${window.location.hostname}:${window.location.port || '8002'}
                        `;
                        throw new Error(solutionMsg);
                    }
                }
                throw getUserMediaError;
            }

            // 立即停止流，我们只是为了获取权限
            stream.getTracks().forEach(track => track.stop());

            console.log('✅ 麦克风权限申请成功');
            this.showPermissionStatus('已授予', '麦克风权限正常，您可以开始语音输入');

            // 验证音频流质量
            this.validateAudioStream(stream);

            // 立即停止流，我们只是为了获取权限
            stream.getTracks().forEach(track => track.stop());

        } catch (error) {
            console.error('❌ 麦克风权限申请失败:', error);

            // 根据错误类型提供针对性的指导
            let errorMessage = '';
            let errorType = '已拒绝';

            if (error.name === 'NotAllowedError') {
                errorMessage = '麦克风权限被拒绝。请在浏览器弹窗中点击"允许"，或在浏览器设置中启用麦克风访问权限。';
                errorType = '已拒绝';
            } else if (error.name === 'NotFoundError') {
                errorMessage = '未找到麦克风设备。请检查麦克风是否正确连接到电脑。';
                errorType = '无设备';
            } else if (error.name === 'NotReadableError') {
                errorMessage = '麦克风被其他应用占用。请关闭其他使用麦克风的应用（如视频会议软件）后重试。';
                errorType = '设备忙碌';
            } else if (error.name === 'OverconstrainedError') {
                errorMessage = '麦克风不支持请求的音频格式。系统将尝试使用兼容的音频设置。';
                errorType = '格式不支持';
                // 对于OverconstrainedError，可以尝试更宽松的约束
                console.log('🔄 尝试使用更宽松的音频约束...');
                setTimeout(() => this.requestMicrophonePermissionWithFallback(), 1000);
                return;
            } else if (error.name === 'SecurityError' || error.message.includes('secure context')) {
                // 检查是否是内网 IP 的安全上下文问题
                const isPrivateNetwork = window.location.hostname.startsWith('192.168.') ||
                                        window.location.hostname.startsWith('10.') ||
                                        window.location.hostname.startsWith('172.');
                const isLocalhost = window.location.hostname === 'localhost' || 
                                   window.location.hostname === '127.0.0.1';
                
                if (isPrivateNetwork && !window.isSecureContext) {
                    errorMessage = `Chrome 浏览器安全策略：内网 IP 地址（${window.location.hostname}）在 HTTP 下无法访问麦克风。

解决方案（任选其一）：
1. 使用 localhost 访问：http://localhost:${window.location.port || '8002'}${window.location.pathname}
2. Chrome 设置：访问 chrome://flags/#unsafely-treat-insecure-origin-as-secure
   添加地址：http://${window.location.hostname}:${window.location.port || '8002'}
   然后重启 Chrome
3. 使用 HTTPS（推荐生产环境）`;
                    errorType = '安全限制';
                } else {
                    errorMessage = '安全错误：请确保网站使用HTTPS协议访问，或使用 localhost 访问。';
                    errorType = '安全错误';
                }
            } else if (error.name === 'AbortError') {
                errorMessage = '请求被中断，请重试。';
                errorType = '已中断';
            } else {
                // 检查错误消息中是否包含安全上下文相关的提示
                if (error.message && error.message.includes('secure context')) {
                    const isPrivateNetwork = window.location.hostname.startsWith('192.168.') ||
                                            window.location.hostname.startsWith('10.') ||
                                            window.location.hostname.startsWith('172.');
                    if (isPrivateNetwork) {
                        errorMessage = `Chrome 浏览器安全策略限制：内网 IP 地址在 HTTP 下无法访问麦克风。\n\n请使用 localhost 访问：http://localhost:${window.location.port || '8002'}${window.location.pathname}`;
                        errorType = '安全限制';
                    } else {
                        errorMessage = `麦克风访问失败: ${error.message}`;
                    }
                } else {
                    errorMessage = `麦克风访问失败: ${error.message}`;
                }
                errorType = '未知错误';
            }

            this.showPermissionStatus(errorType, errorMessage);
        }
    }

    // 处理权限状态变化
    handlePermissionChange(newState) {
        console.log('处理权限状态变化:', newState);

        switch (newState) {
            case 'granted':
                this.showPermissionStatus('已授予', '麦克风权限已恢复，您可以开始语音输入');
                break;

            case 'denied':
                this.showPermissionStatus('已拒绝', '麦克风权限已被撤销，请在浏览器设置中重新允许访问麦克风');
                break;

            default:
                console.log('权限状态变化（未处理）:', newState);
        }
    }

    // 显示权限状态
    showPermissionStatus(status, message) {
        // 在控制台显示
        console.log(`麦克风权限状态: ${status} - ${message}`);

        // 发送权限状态到后端
        this.sendPermissionStatusToBackend(status, message);

        // 获取权限指示器元素
        const indicator = document.getElementById('permissionIndicator');
        const icon = document.getElementById('permissionIcon');
        const text = document.getElementById('permissionText');

        if (!indicator || !icon || !text) {
            console.warn('权限指示器元素未找到');
            return;
        }

        // 显示指示器
        indicator.style.display = 'flex';

        // 根据状态设置样式和内容
        let cssClass, iconClass, displayText;
        switch (status) {
            case '已授予':
                cssClass = 'granted';
                iconClass = 'fas fa-microphone';
                displayText = '权限正常';
                break;
            case '已拒绝':
                cssClass = 'denied';
                iconClass = 'fas fa-microphone-slash';
                displayText = '权限被拒';
                break;
            case '申请中':
                cssClass = 'pending';
                iconClass = 'fas fa-spinner fa-spin';
                displayText = '申请权限';
                break;
            case '检查失败':
                cssClass = 'denied';
                iconClass = 'fas fa-exclamation-triangle';
                displayText = '检查失败';
                break;
            case '待检查':
                cssClass = 'unknown';
                iconClass = 'fas fa-clock';
                displayText = '权限检查中';
                break;
            default:
                cssClass = 'unknown';
                iconClass = 'fas fa-question-circle';
                displayText = '权限未知';
        }

        // 移除旧的CSS类
        indicator.classList.remove('granted', 'denied', 'pending', 'unknown');

        // 添加新的CSS类
        indicator.classList.add(cssClass);

        // 设置图标和文本
        icon.className = iconClass;
        text.textContent = displayText;

        // 设置title属性显示详细信息
        indicator.title = `${status}: ${message}`;

        // 对于成功状态，3秒后隐藏指示器
        if (status === '已授予') {
            setTimeout(() => {
                if (indicator) {
                    indicator.style.display = 'none';
                }
            }, 3000);
        }
    }

    // 加载用户信息
    async loadUserInfo() {
        try {
            const user = this.currentUser;

            // 从invitationData获取用户名、岗位和面试时长
            if (user.invitation_data) {
                // 显示组织信息（用户名和岗位）
                this.displayOrganizationInfo(user.invitation_data);

                // 根据邀请配置设置面试总时长：basic_info_duration + professional_duration（后端单位：分钟，前端倒计时用秒）
                try {
                    const basicMinutes = Number(user.invitation_data.basic_info_duration || 0);
                    const professionalMinutes = Number(user.invitation_data.professional_duration || 0);
                    const totalMinutes = basicMinutes + professionalMinutes;

                    if (totalMinutes > 0) {
                        this.INTERVIEW_DURATION = totalMinutes * 60; // 分钟 -> 秒
                        this.interviewRemainingTime = this.INTERVIEW_DURATION;
                        console.log(`✅ 已根据邀请配置更新面试总时长: ${totalMinutes} 分钟（${this.INTERVIEW_DURATION} 秒），basic=${basicMinutes}min, professional=${professionalMinutes}min`);
                    } else {
                        console.warn('邀请配置中的时长为空或无效，继续使用默认面试时长（40分钟）');
                    }
                } catch (e) {
                    console.error('解析邀请面试时长失败，使用默认40分钟:', e);
                }
            }
        } catch (error) {
            console.error('加载用户信息失败:', error);
        }
    }

    // 显示组织信息
    displayOrganizationInfo(invitationData) {
        try {
            const usernameSpan = document.getElementById('usernameDisplay');
            const positionSpan = document.getElementById('positionDisplay');

            if (usernameSpan && positionSpan) {
                usernameSpan.textContent = `用户名: ${invitationData.candidate_name || '未设置'}`;
                positionSpan.textContent = `面试岗位: ${invitationData.position || '未设置'}`;
            }
        } catch (error) {
            console.error('显示组织信息失败:', error);
        }
    }


    // 添加组织信息样式
    addOrganizationInfoStyles() {
        if (document.getElementById('org-info-styles')) {
            return; // 样式已添加
        }

        const style = document.createElement('style');
        style.id = 'org-info-styles';
        style.textContent = `
            .organization-info {
                margin: 8px 0 12px 0;
                padding: 12px;
                background: rgba(102, 126, 234, 0.1);
                border-radius: 8px;
                border: 1px solid rgba(102, 126, 234, 0.2);
            }

            .org-item {
                display: flex;
                align-items: center;
                gap: 8px;
                margin: 6px 0;
                font-size: 13px;
                color: #333;
            }

            .org-item i {
                color: #667eea;
                width: 16px;
                text-align: center;
            }

            .org-item:first-child {
                animation: fadeInUp 0.3s ease-out;
            }

            .org-item:nth-child(2) {
                animation: fadeInUp 0.3s ease-out 0.1s both;
            }

            @keyframes fadeInUp {
                from {
                    opacity: 0;
                    transform: translateY(10px);
                }
                to {
                    opacity: 1;
                    transform: translateY(0);
                }
            }
        `;
        document.head.appendChild(style);
    }


    // 初始化UI
    initializeUI() {
        // 设置初始状态
        this.updateStatus('未开始', 'pending');
        this.updateSessionId('-');
        this.updateProgress(0, 0);

        // 隐藏所有模态框
        this.hideAllModals();

        // 初始化输入框
        this.initializeInput();
    }
    
    // 初始化输入框 - 只支持语音输入
    initializeInput() {
        // 移除文本输入初始化逻辑，只保留语音输入相关初始化
        console.log('只支持语音输入，跳过文本输入初始化');
    }
    
    // ==================== 聊天消息相关 ====================
    addMessage(text, type = 'bot', audioUrl = null, messageType = null, autoScroll = true) {
        const chatMessages = document.getElementById('chatMessages');
        const messageDiv = document.createElement('div');
        messageDiv.className = `message ${type}-message${messageType ? ` ${messageType}-message` : ''}`;
        
        const avatar = document.createElement('div');
        avatar.className = `message-avatar ${type}-avatar`;
        if (type === 'bot') {
            avatar.innerHTML = '<i class="fas fa-robot"></i>';
        } else {
            avatar.innerHTML = `<span>${this.currentUser?.username?.charAt(0).toUpperCase() || 'U'}</span>`;
        }
        
        const content = document.createElement('div');
        content.className = 'message-content';
        
        const bubble = document.createElement('div');
        bubble.className = `message-bubble ${type}-bubble`;
        
        // 添加文本内容
        const textParts = text.split('\n');
        textParts.forEach(part => {
            if (part.trim()) {
                const p = document.createElement('p');
                p.textContent = part;
                bubble.appendChild(p);
            }
        });
        
        // 添加音频播放器（如果有）
        if (audioUrl && type === 'bot') {
            const audioWrapper = document.createElement('div');
            audioWrapper.style.marginTop = '10px';
            const audio = document.createElement('audio');
            audio.src = audioUrl;
            audio.controls = true;
            audio.style.width = '100%';
            audio.style.maxWidth = '300px';
            audioWrapper.appendChild(audio);
            bubble.appendChild(audioWrapper);
        }
        
        const time = document.createElement('div');
        time.className = 'message-time';
        time.textContent = this.formatTime(new Date());
        
        content.appendChild(bubble);
        content.appendChild(time);
        
        messageDiv.appendChild(avatar);
        messageDiv.appendChild(content);
        
        chatMessages.appendChild(messageDiv);
        
        // 第一题加载时，不自动滚动到底部，而是保持顶部位置
        if (autoScroll && this.currentQuestionIndex !== 0) {
            this.scrollToBottom();
        }
    }
    
    addBotMessage(text, audioUrl = null, messageType = null, autoScroll = true) {
        this.addMessage(text, 'bot', audioUrl, messageType, autoScroll);
    }
    
    // 添加追问问题消息（支持HTML格式）
    addFollowUpMessage(questionText) {
        const chatMessages = document.getElementById('chatMessages');
        const messageDiv = document.createElement('div');
        messageDiv.className = 'message bot-message';
        
        const avatar = document.createElement('div');
        avatar.className = 'message-avatar bot-avatar';
        avatar.innerHTML = '<i class="fas fa-robot"></i>';
        
        const content = document.createElement('div');
        content.className = 'message-content';
        
        const bubble = document.createElement('div');
        bubble.className = 'message-bubble bot-bubble';
        
        // 使用innerHTML支持HTML格式
        const p = document.createElement('p');
        p.innerHTML = `<span style="color: #ff6b6b; font-weight: bold;">【追问】</span> ${questionText}`;
        bubble.appendChild(p);
        
        const time = document.createElement('div');
        time.className = 'message-time';
        time.textContent = this.formatTime(new Date());
        
        content.appendChild(bubble);
        content.appendChild(time);
        
        messageDiv.appendChild(avatar);
        messageDiv.appendChild(content);
        
        chatMessages.appendChild(messageDiv);
        this.scrollToBottom();
    }
    
    addUserMessage(text) {
        this.addMessage(text, 'user');
    }
    
    scrollToBottom() {
        // 移动端：滚动容器是 .question-content，不是 chatMessages
        const questionContent = document.getElementById('questionContent');
        const chatMessages = document.getElementById('chatMessages');
        
        if (questionContent) {
            // 移动端：在 question-content 容器上滚动
            // 使用 requestAnimationFrame 确保DOM更新后再滚动
            requestAnimationFrame(() => {
                questionContent.scrollTop = questionContent.scrollHeight;
            });
        } else if (chatMessages) {
            // 桌面端：在 chatMessages 上滚动（兼容旧代码）
            requestAnimationFrame(() => {
                chatMessages.scrollTop = chatMessages.scrollHeight;
            });
        }
    }
    
    scrollToTop() {
        // 滚动到顶部，让用户看到完整内容
        const questionContent = document.getElementById('questionContent');
        const chatMessages = document.getElementById('chatMessages');
        
        if (questionContent) {
            // 移动端：在 question-content 容器上滚动到顶部
            requestAnimationFrame(() => {
                questionContent.scrollTop = 0;
            });
        } else if (chatMessages) {
            // 桌面端：在 chatMessages 上滚动到顶部（兼容旧代码）
            requestAnimationFrame(() => {
                chatMessages.scrollTop = 0;
            });
        }
    }
    
    ensureQuestionAreaScrollable() {
        // 确保题目区域可以滚动，并滚动到顶部
        const questionContent = document.getElementById('questionContent');
        const questionSection = document.querySelector('.question-section');
        const chatMessages = document.getElementById('chatMessages');
        
        if (questionContent && questionSection) {
            // 强制设置滚动容器的样式，确保可以滚动
            const computedStyle = window.getComputedStyle(questionContent);
            const sectionStyle = window.getComputedStyle(questionSection);
            
            console.log('🔍 检查题目区域滚动能力:');
            console.log('  questionSection height:', sectionStyle.height);
            console.log('  questionSection flex:', sectionStyle.flex);
            console.log('  questionContent height:', computedStyle.height);
            console.log('  questionContent overflow-y:', computedStyle.overflowY);
            console.log('  questionContent min-height:', computedStyle.minHeight);
            console.log('  questionContent scrollHeight:', questionContent.scrollHeight);
            console.log('  questionContent clientHeight:', questionContent.clientHeight);
            console.log('  chatMessages scrollHeight:', chatMessages?.scrollHeight);
            
            // 确保overflow-y是auto或scroll
            if (computedStyle.overflowY !== 'auto' && computedStyle.overflowY !== 'scroll') {
                questionContent.style.overflowY = 'auto';
                console.log('✅ 强制设置 overflow-y: auto');
            }
            
            // 确保min-height是0（关键！）
            if (computedStyle.minHeight !== '0px') {
                questionContent.style.minHeight = '0';
                console.log('✅ 强制设置 min-height: 0');
            }
            
            // 确保question-section有正确的高度
            if (sectionStyle.height === 'auto' || sectionStyle.height === '0px') {
                // 强制重新计算布局
                questionSection.style.display = 'flex';
                console.log('✅ 强制设置 question-section display: flex');
            }
            
            // 等待布局稳定后再滚动
            setTimeout(() => {
                // 再次检查
                const newScrollHeight = questionContent.scrollHeight;
                const newClientHeight = questionContent.clientHeight;
                
                console.log('🔍 延迟检查后:');
                console.log('  scrollHeight:', newScrollHeight);
                console.log('  clientHeight:', newClientHeight);
                console.log('  可以滚动:', newScrollHeight > newClientHeight);
                
                // 滚动到顶部
                questionContent.scrollTop = 0;
                console.log('✅ 滚动到顶部，scrollTop:', questionContent.scrollTop);
                
                // 如果还是无法滚动，尝试强制设置高度
                if (newScrollHeight <= newClientHeight && newScrollHeight > 0) {
                    console.warn('⚠️ 内容高度未超过容器，尝试强制设置高度');
                    // 使用实际内容高度
                    const contentHeight = chatMessages ? chatMessages.scrollHeight : newScrollHeight;
                    if (contentHeight > 0) {
                        questionContent.style.height = contentHeight + 'px';
                        console.log('✅ 强制设置高度为内容高度:', contentHeight);
                        // 再次滚动到顶部
                        questionContent.scrollTop = 0;
                    }
                }
            }, 100);
        }
    }
    
    formatTime(date) {
        const now = new Date();
        const diff = now - date;
        const minutes = Math.floor(diff / 60000);
        
        if (minutes < 1) return '刚刚';
        if (minutes < 60) return `${minutes}分钟前`;
        
        const hours = Math.floor(minutes / 60);
        if (hours < 24) return `${hours}小时前`;
        
        return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    }

    // 绑定事件监听器
    bindEventListeners() {
        // 页面卸载时清理资源和登录状态
        window.addEventListener('beforeunload', () => {
            this.cleanup();
            // 清除登录状态，确保下次打开页面需要重新登录
            localStorage.removeItem('isLoggedIn');
            localStorage.removeItem('userData');
            localStorage.removeItem('invitationData');
        });

        // 键盘快捷键 - 只支持语音录制
        document.addEventListener('keydown', (e) => {
            // 空格键开始/停止录制
            if (e.target.tagName !== 'INPUT' && e.target.tagName !== 'TEXTAREA') {
                if (e.code === 'Space') {
                    e.preventDefault();
                    if (this.mediaRecorder && this.mediaRecorder.state === 'recording') {
                        this.stopRecording();
                    } else {
                        this.startRecording();
                    }
                }
            }
        });

        // 绑定按钮事件监听器（确保在JavaScript加载完成后）
        this.bindButtonEvents();
    }

    // 绑定按钮事件监听器
    bindButtonEvents() {
        // 防止重复绑定
        if (this.eventsBound) {
            console.log('ℹ️ 事件已绑定，跳过重复绑定');
            return;
        }

        // 开始回答 / 回答完毕下一题 同一按钮：未录音时开始录音，录音中时下一题（与旧代码逻辑一致）
        const startAnswerBtn = document.getElementById('startAnswerBtn');
        if (startAnswerBtn && !startAnswerBtn.hasAttribute('data-event-bound')) {
            startAnswerBtn.addEventListener('click', () => {
                if (window.interviewManager && window.interviewManager.isRecording) {
                    console.log('🎯 当前录音中，执行回答完毕下一题');
                    if (typeof finishAnswer === 'function') finishAnswer();
                } else {
                    console.log('🎯 未在录音，执行开始回答（连接WS并开始录音）');
                    if (window.interviewManager && typeof window.interviewManager.startAnswer === 'function') {
                        window.interviewManager.startAnswer();
                    } else if (typeof startAnswer === 'function') {
                        startAnswer();
                    } else {
                        console.error('❌ startAnswer 不可用');
                        alert('系统初始化未完成，请刷新页面重试');
                    }
                }
            });
            startAnswerBtn.setAttribute('data-event-bound', 'true');
            console.log('✅ 开始回答/回答完毕按钮事件绑定成功');
        }

        this.eventsBound = true;
    }

    // ==================== 用户认证相关 ====================


    // ==================== 面试会话相关 ====================
    async startInterview() {
        if (this.interviewStarted) {
            console.log('⚠️ 面试已经开始，跳过重复调用');
            return;
        }
        this.interviewStarted = true;

        try {
            console.log('=== 开始面试流程 === (首次调用)');
            this.showLoading('开始面试', '正在准备面试环境');

            // 从localStorage获取邀请ID
            const invitationDataStr = localStorage.getItem('invitationData');
            console.log('原始邀请数据字符串:', invitationDataStr);

            const invitationData = JSON.parse(invitationDataStr || '{}');
            console.log('解析后的邀请数据:', invitationData);

            const invitationId = invitationData.invitation_id || '';
            console.log('提取的邀请ID:', invitationId);

            if (!invitationId) {
                console.error('邀请ID不存在');
                throw new Error('邀请ID不存在，无法开始面试');
            }

            console.log('邀请ID验证通过:', invitationId);
            this.currentInvitationId = invitationId;

            // 同时获取基本题目和专业题目列表
            const [basicQuestions, professionalQuestions] = await Promise.all([
                window.API.interview.getBasicQuestions(invitationId),
                window.API.interview.getProfessionalQuestions(invitationId)
            ]);

            // 存储题目列表到session中
            this.basicQuestions = basicQuestions || [];
            this.professionalQuestions = professionalQuestions || [];

            // 将所有题目合并到一个列表中，basicQuestions 在前，professionalQuestions 在后
            this.allQuestions = [
                // 先添加所有基本题目
                ...this.basicQuestions.map(id => ({ question_id: id, type: 'basic' })),
                // 再添加所有专业题目
                ...this.professionalQuestions.map(id => ({ question_id: id, type: 'professional' }))
            ];

            this.totalQuestions = this.allQuestions.length;
            this.currentQuestionIndex = 0; // 初始化当前题目索引

            console.log('基本题目数量:', this.basicQuestions.length);
            console.log('专业题目数量:', this.professionalQuestions.length);
            console.log('总题目数量:', this.totalQuestions);
            console.log('合并题目列表:', this.allQuestions);

            // 开始面试流程（包含创建会话和初始化题目）
            const interviewResult = await window.API.interview.startInterview(invitationId);
            
            // 深度排查：打印完整的 interviewResult
            console.log('🔍 [深度排查] interviewResult 完整对象:', JSON.stringify(interviewResult, null, 2));
            if (window.addDebugLog) window.addDebugLog(`🔍 [深度排查] interviewResult: ${JSON.stringify(interviewResult)}`, 'info');
            console.log('🔍 [深度排查] interviewResult.data:', interviewResult.data);
            console.log('🔍 [深度排查] interviewResult.data.current_question:', interviewResult.data?.current_question);
            if (window.addDebugLog) window.addDebugLog(`🔍 [深度排查] current_question: ${JSON.stringify(interviewResult.data?.current_question)}`, 'info');

            if (!interviewResult.success) {
                throw new Error(interviewResult.message || '开始面试失败');
            }

            // 保存会话ID（仅使用本次 start 返回的 session_id，避免残留旧 ID 导致 complete 404）
            this.currentSession = {
                session_id: interviewResult.data.session_id,
                invitation_id: invitationId
            };
            this.updateSessionId(this.currentSession.session_id);
            // 标记本页已成功开始面试，beforeunload 时仅在此情况下才调用 complete
            try { window.__interviewStartedInThisPage = true; } catch (e) {}

            // 更新UI状态
            this.updateStatus('进行中', 'active');

            // 隐藏开始面试按钮（在聊天区域中的大按钮）
            const startWrapper = document.getElementById('startInterviewWrapper');
            if (startWrapper) {
                startWrapper.style.display = 'none';
            }
            
            // 启动面试倒计时（40分钟，参考旧代码）
            this.startInterviewCountdown();

            // 更新按钮状态（开始面试按钮已移除）
            const pauseBtn = document.getElementById('pauseInterviewBtn');
            const resumeBtn = document.getElementById('resumeInterviewBtn');
            const startAnswerBtn = document.getElementById('startAnswerBtn');
            const endInterviewBtn = document.getElementById('endInterviewBtn');

            if (pauseBtn) pauseBtn.disabled = false;
            if (resumeBtn) resumeBtn.disabled = true;
            if (startAnswerBtn) startAnswerBtn.disabled = false;
            if (endInterviewBtn) endInterviewBtn.disabled = false;

            this.hideLoading();

            // 显示面试官介绍 + 题目总数（先显示介绍）
            // 第一题加载时，不自动滚动，保持顶部位置
            this.addBotMessage(`我是您的AI面试官，将为您进行面试。本次面试共有 ${this.totalQuestions} 个问题（基本题：${this.basicQuestions.length} 个，专业题：${this.professionalQuestions.length} 个）。请阅读问题并回答。`, null, null, false);

            // 显示第一个问题（后显示问题）
            console.log('🔍 startInterview: 检查 interviewResult.data:', interviewResult.data);
            if (interviewResult.data && interviewResult.data.current_question) {
                console.log('✅ startInterview: 找到 current_question，准备调用 updateCurrentQuestion');
                if (window.addDebugLog) window.addDebugLog('✅ startInterview: 找到 current_question，准备调用 updateCurrentQuestion', 'info');
                try {
                    this.updateCurrentQuestion(interviewResult.data.current_question);
                    console.log('✅ startInterview: updateCurrentQuestion 调用完成');
                } catch (error) {
                    console.error('❌ startInterview: updateCurrentQuestion 调用失败:', error);
                    if (window.addDebugLog) window.addDebugLog(`❌ startInterview: updateCurrentQuestion 调用失败: ${error.message}`, 'error');
                }
                this.updateProgress(
                    interviewResult.data.current_index,
                    interviewResult.data.total_questions || this.totalQuestions
                );
            } else {
                console.warn('⚠️ startInterview: interviewResult.data 或 current_question 不存在');
                console.log('interviewResult:', interviewResult);
                if (window.addDebugLog) window.addDebugLog('⚠️ startInterview: interviewResult.data 或 current_question 不存在', 'warning');
            }
            
            // 第一题加载完成后，确保滚动到顶部并可以滚动
            setTimeout(() => {
                this.ensureQuestionAreaScrollable();
            }, 500);

            // 暂时屏蔽语音播放，直接启用输入控件
            console.log('语音播放功能已暂时屏蔽，直接启用文本输入');
            this.enableInputControls();

            // 步骤4（业务流程）：自动建立 WebSocket 并启动录音，无需用户点击「开始回答」
            // 增加延迟时间，确保题目和WebSocket都初始化完成（特别是第一题）
            console.log('📌 步骤4：约 2 秒后自动启动录音（业务流程：自动开始面试）');
            const self = this;
            let retryCount = 0;
            const maxRetries = 5; // 最多重试5次
            
            function autoStartRecording() {
                if (self.isRecording) {
                    console.log('⏭ 已在录音，跳过自动启动');
                    return;
                }
                if (!self.currentQuestion || !self.currentQuestion.question_id) {
                    retryCount++;
                    if (retryCount < maxRetries) {
                        console.warn(`⏭ 当前无题目，${retryCount}/${maxRetries} 秒后重试自动启动录音`);
                        setTimeout(autoStartRecording, 1000);
                        return;
                    } else {
                        console.warn('⏭ 重试次数已达上限，跳过自动启动录音，等待用户手动点击');
                        return;
                    }
                }
                console.log('🎤 自动启动录音（业务流程）');
                self.startAnswer().catch(function (err) {
                    console.error('自动启动录音失败:', err);
                    // 如果自动启动失败，不阻止用户手动点击
                });
            }
            
            setTimeout(autoStartRecording, 2000); // 增加到2秒，给更多初始化时间

        } catch (error) {
            console.error('开始面试失败:', error);
            this.hideLoading();
            this.showError('开始面试失败: ' + error.message);
        }
    }

    async pauseInterview() {
        try {
            this.showLoading('暂停面试', '正在暂停面试');
            
            const result = await window.API.interview.pauseInterview(this.currentSession.session_id);
            
            if (result.success) {
                this.updateStatus('已暂停', 'paused');
                this.updateButtonStates('paused');
                this.showSuccess('面试已暂停');
            }
            
            this.hideLoading();
            
        } catch (error) {
            console.error('暂停面试失败:', error);
            this.hideLoading();
            this.showError('暂停面试失败: ' + error.message);
        }
    }

    async resumeInterview() {
        try {
            this.showLoading('恢复面试', '正在恢复面试');
            
            const result = await window.API.interview.resumeInterview(this.currentSession.session_id);
            
            if (result.success) {
                this.updateStatus('进行中', 'active');
                this.updateButtonStates('active');
                this.showSuccess('面试已恢复');
            }
            
            this.hideLoading();
            
        } catch (error) {
            console.error('恢复面试失败:', error);
            this.hideLoading();
            this.showError('恢复面试失败: ' + error.message);
        }
    }

    async completeInterview() {
        try {
            // 移动端友好的确认方式
            let confirmed = false;
            if (window.innerWidth <= 768) {
                // 移动端：使用自定义确认对话框
                confirmed = await this.showMobileConfirm('确定要完成面试吗？');
            } else {
                // 桌面端：使用浏览器confirm
                confirmed = confirm('确定要完成面试吗？');
            }
            
            if (!confirmed) {
                return;
            }

            try {
                await this.handleInterviewCompleted({ durationMinutes: null });
            } finally {
                this.hideLoading();
            }

        } catch (error) {
            console.error('完成面试失败:', error);
            this.hideLoading();
            this.showError('完成面试失败: ' + error.message);
        }
    }

    // 移动端友好的确认对话框
    showMobileConfirm(message) {
        return new Promise((resolve) => {
            const confirmHTML = `
                <div id="mobileConfirmDialog" style="position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); z-index: 9999; display: flex; align-items: center; justify-content: center; padding: 20px;">
                    <div style="background: white; padding: 25px; border-radius: 12px; max-width: 90%; width: 100%; max-width: 400px; text-align: center; box-shadow: 0 4px 20px rgba(0,0,0,0.3);">
                        <p style="font-size: 18px; margin-bottom: 25px; color: #333; line-height: 1.5;">${message}</p>
                        <div style="display: flex; gap: 12px; justify-content: center;">
                            <button id="confirmCancel" style="flex: 1; padding: 12px; background: #6c757d; color: white; border: none; border-radius: 8px; font-size: 16px; cursor: pointer;">取消</button>
                            <button id="confirmOk" style="flex: 1; padding: 12px; background: #dc3545; color: white; border: none; border-radius: 8px; font-size: 16px; cursor: pointer;">确定</button>
                        </div>
                    </div>
                </div>
            `;
            
            // 移除旧的对话框
            const oldDialog = document.getElementById('mobileConfirmDialog');
            if (oldDialog) {
                oldDialog.remove();
            }
            
            // 添加新对话框
            document.body.insertAdjacentHTML('beforeend', confirmHTML);
            
            // 绑定事件
            document.getElementById('confirmOk').addEventListener('click', () => {
                document.getElementById('mobileConfirmDialog').remove();
                resolve(true);
            });
            
            document.getElementById('confirmCancel').addEventListener('click', () => {
                document.getElementById('mobileConfirmDialog').remove();
                resolve(false);
            });
        });
    }

    // 停止所有录音
    async stopAllRecording() {
        console.log('停止所有录音和检测');

        // 停止音量检测
        this.stopVolumeMonitoring();

        // 停止实时转录
        if (this.transcriptionInterval) {
            clearInterval(this.transcriptionInterval);
            this.transcriptionInterval = null;
        }

        // 停止当前题目录音
        if (this.currentQuestionRecorder && this.currentQuestionRecorder.state === 'recording') {
            await this.stopCurrentQuestionRecording();
        }

        // 停止全程录音
        if (this.fullRecordingRecorder && this.fullRecordingRecorder.state === 'recording') {
            await this.stopFullRecording();
        }

        // 停止音频流
        if (this.stream) {
            this.stream.getTracks().forEach(track => track.stop());
            this.stream = null;
        }
    }

    // 保存全程录音
    async saveFullRecording() {
        try {
            if (this.fullRecordingChunks.length === 0) {
                console.log('没有全程录音数据');
                return;
            }

            const fullAudioBlob = new Blob(this.fullRecordingChunks, {
                type: 'audio/webm;codecs=opus'
            });

            console.log(`全程录音Blob大小: ${fullAudioBlob.size} bytes`);

            // 可以在这里添加上传全程录音到服务器的逻辑
            // 暂时只记录日志
            console.log('全程录音已准备就绪，可用于后续上传或本地保存');

            // 示例：下载到本地（可选）
            // const url = URL.createObjectURL(fullAudioBlob);
            // const a = document.createElement('a');
            // a.href = url;
            // a.download = `interview_${this.currentSession.session_id}_full.webm`;
            // a.click();
            // URL.revokeObjectURL(url);

        } catch (error) {
            console.error('保存全程录音失败:', error);
        }
    }

    // ==================== 问题相关 ====================
    async getNextQuestion() {
        try {
            if (!this.currentSession) {
                this.showError('请先开始面试');
                return;
            }
            
            this.showLoading('获取问题', '正在获取下一个问题');
            
            const result = await window.API.interview.getNextQuestion(this.currentSession.session_id);
            
            if (result.success) {
                if (result.data.is_completed) {
                    this.updateStatus('已完成', 'completed');
                    this.updateButtonStates('completed');
                    this.showSuccess('所有问题已回答完成！');
                } else {
                    this.updateCurrentQuestion(result.data.current_question);
                    this.updateProgress(result.data.current_index, result.data.total_questions);
                    // 暂时屏蔽语音播放，直接启用输入控件
                    console.log('语音播放功能已暂时屏蔽，直接启用文本输入');
                    this.enableInputControls();
                }
            }
            
            this.hideLoading();
            
        } catch (error) {
            console.error('获取下一问题失败:', error);
            this.hideLoading();
            this.showError('获取下一问题失败: ' + error.message);
        }
    }

    /** 收到 WebSocket next_question 时若本地无题目数据，则拉取当前题并更新 */
    async getCurrentQuestionAndUpdate() {
        try {
            // 获取新题目时解锁追问状态
            if (this.isFollowUpActive) {
                console.log('🔓 获取新题目，解除追问状态锁定');
                this.isFollowUpActive = false;
            }
            if (!this.currentSession) return;
            const result = await window.API.interview.getCurrentQuestion(
                this.currentSession.session_id,
                this.currentSession.invitation_id || this.currentInvitationId
            );
            if (result && (result.text || result.question_id)) {
                const q = { question_id: result.question_id, question_text: result.text || '' };
                this.updateCurrentQuestion(q);
                this.enableInputControls();
            }
        } catch (e) {
            console.warn('getCurrentQuestionAndUpdate 失败:', e);
        }
    }

    async playCurrentQuestion() {
        // 暂时屏蔽语音播放功能，只显示文本
        console.log('语音播放功能已暂时屏蔽，只显示题目文本');
        // 直接启用输入控件，让用户可以开始回答
        this.enableInputControls();
    }

    updateCurrentQuestion(question) {
        console.log('🔍 updateCurrentQuestion 被调用，question:', question);
        if (window.addDebugLog) window.addDebugLog(`🔍 updateCurrentQuestion 被调用，question: ${JSON.stringify(question)}`, 'info');
        
        if (!question) {
            console.error('❌ updateCurrentQuestion: question 参数为空');
            if (window.addDebugLog) window.addDebugLog('❌ updateCurrentQuestion: question 参数为空', 'error');
            return;
        }
        
        this.currentQuestion = question;
        if (question && this.wsASRClient && question.question_id) {
            if (question.question_id !== this.wsASRClient.questionId) {
                this.wsASRClient.switchQuestion(question.question_id);
            }
        }

        // 更新当前题目索引（如果后端返回了question_id）
        if (question && question.question_id) {
            const index = this.allQuestions.findIndex(item => item.question_id === question.question_id);
            if (index !== -1) {
                this.currentQuestionIndex = index;
            }
        }

        // 更新进度条（每次切换题目时更新）
        if (this.totalQuestions > 0) {
            const currentNumber = this.currentQuestionIndex + 1;
            this.updateProgress(currentNumber, this.totalQuestions);
        }

        // 重置当前题目相关的状态
        this.resetCurrentQuestionState();

        // 在聊天界面显示问题（只显示问题内容）；避免 API 同步与 WS 重复触发时同一题干出现两次
        if (question && question.question_text) {
            const qid = question.question_id;
            if (qid && this._lastDisplayedQuestionBubbleId === qid) {
                console.debug('updateCurrentQuestion: 本题题干气泡已展示，跳过重复 addBotMessage', qid);
            } else {
                if (qid) {
                    this._lastDisplayedQuestionBubbleId = qid;
                }
                const questionNumber = this.currentQuestionIndex + 1;
                const questionText = question.question_text; // 只显示问题内容，不显示编号前缀

                console.log(`准备题目 ${questionNumber}:`, question.question_text);

                // 暂时屏蔽TTS功能，直接启用输入控件
                this.generateAndStoreQuestionAudio(question.question_text);

                // 只显示问题文本
                // 第一题加载时，不自动滚动到底部
                this.addBotMessage(questionText, null, null, this.currentQuestionIndex !== 0);
            }
            
            // 第一题加载时，延迟滚动到顶部，让用户看到完整内容（包括介绍和题目）
            if (this.currentQuestionIndex === 0) {
                setTimeout(() => {
                    this.ensureQuestionAreaScrollable();
                    console.log('✅ 第一题：确保题目区域可滚动并滚动到顶部');
                }, 300); // 增加延迟，确保DOM完全渲染
            }
        }
        
        // 确保转写容器显示（特别是第一题）
        const currentQuestionNum = this.currentQuestionIndex + 1;
        
        // 检查并确保 transcription-section 显示（不强制修改flex，让CSS布局自然工作）
        const transcriptionSection = document.querySelector('.transcription-section');
        if (transcriptionSection) {
            // 只确保display不为none，不强制修改flex值
            const currentDisplay = window.getComputedStyle(transcriptionSection).display;
            if (currentDisplay === 'none') {
                transcriptionSection.style.display = 'flex';
                console.log(`✅ [题目${currentQuestionNum}] transcription-section 从隐藏状态恢复显示`);
            }
            
            // 检查元素是否在可视区域内
            const rect = transcriptionSection.getBoundingClientRect();
            const viewportHeight = window.innerHeight;
            const isInViewport = rect.top < viewportHeight && rect.bottom > 0;
            
            if (!isInViewport) {
                console.warn(`⚠️ [题目${currentQuestionNum}] transcription-section 不在可视区域内: top=${rect.top}, viewportHeight=${viewportHeight}`);
                // 如果不在可视区域内，尝试滚动到该元素
                transcriptionSection.scrollIntoView({ behavior: 'smooth', block: 'end' });
            }
        }
        
        // 确保 transcription-container 显示
        const transcriptionContainer = document.getElementById('transcriptionContainer');
        if (transcriptionContainer) {
            // 移除内联的 display: none
            if (transcriptionContainer.style.display === 'none') {
                transcriptionContainer.style.display = 'flex';
            }
            
            // 如果容器是空的，显示空状态提示
            const emptyTextEl = document.getElementById('emptyText');
            const transcriptText = document.getElementById('transcriptText');
            const intermediateTextEl = document.getElementById('intermediateText');
            if (emptyTextEl && (!transcriptText?.textContent && !intermediateTextEl?.textContent)) {
                emptyTextEl.style.display = 'block';
                emptyTextEl.textContent = '等待语音输入...';
            }
        }
    }

    // 启动面试倒计时（40分钟，参考旧代码）
    startInterviewCountdown() {
        // 清除之前的倒计时
        if (this.interviewTimer) {
            clearInterval(this.interviewTimer);
        }
        
        // 重置剩余时间
        this.interviewRemainingTime = this.INTERVIEW_DURATION;
        
        console.log('⏰ 启动面试倒计时，总时长：40分钟');
        
        // 启动倒计时
        this.interviewTimer = setInterval(() => {
            this.interviewRemainingTime--;
            
            // 更新显示
            this.updateInterviewCountdownDisplay();
            
            // 倒计时结束，自动结束面试
            if (this.interviewRemainingTime <= 0) {
                this.interviewRemainingTime = 0;
                this.stopInterviewCountdown();
                this.showWarning('面试时间已到，面试已自动结束');
                void this.handleInterviewCompleted({}).catch((err) => {
                    console.error('自动结束面试收尾失败:', err);
                });
            }
        }, 1000);
        
        // 立即更新一次显示
        this.updateInterviewCountdownDisplay();
    }
    
    // 停止面试倒计时
    stopInterviewCountdown() {
        if (this.interviewTimer) {
            clearInterval(this.interviewTimer);
            this.interviewTimer = null;
        }
    }
    
    // 格式化倒计时显示：分钟:秒（如 40:00，参考旧代码）
    formatCountdown(seconds) {
        const minutes = Math.floor(seconds / 60);
        const secs = seconds % 60;
        return `${minutes}:${String(secs).padStart(2, '0')}`;
    }

    // 设置倒计时（已移除题目倒计时，只保留40分钟面试倒计时）
    setupCountdown(question) {
        // 题目倒计时已移除，不再执行任何操作
        // 只使用40分钟面试倒计时
    }

    // 更新面试倒计时显示（参考旧代码：根据剩余时间改变颜色）
    updateInterviewCountdownDisplay() {
        const timeString = this.formatCountdown(this.interviewRemainingTime);

        // 更新header中的剩余时间显示
        const timeRemainingElement = document.getElementById('timeRemaining');
        if (timeRemainingElement) {
            timeRemainingElement.textContent = `剩余时间: ${timeString}`;

            // 时间不足时改变颜色（参考旧代码：<=300秒红色，<=600秒橙色，其他绿色）
            if (this.interviewRemainingTime <= 300) {
                timeRemainingElement.style.color = '#dc3545'; // 红色
            } else if (this.interviewRemainingTime <= 600) {
                timeRemainingElement.style.color = '#fd7e14'; // 橙色
            } else {
                timeRemainingElement.style.color = '#28a745'; // 绿色
            }
        }
    }
    
    // 更新题目倒计时显示（已移除，不再使用）
    updateCountdownDisplay() {
        // 题目倒计时已移除，不再执行任何操作
    }

    // 显示倒计时（已移除题目倒计时，不再使用）
    showCountdown() {
        // 题目倒计时已移除，不再执行任何操作
    }

    // 隐藏倒计时（已移除题目倒计时，不再使用）
    hideCountdown() {
        // 题目倒计时已移除，不再执行任何操作
    }

    // 处理时间到的情况（题目倒计时已移除，此方法不再使用）
    handleTimeUp() {
        // 题目倒计时已移除，不再执行任何操作
        // 只使用40分钟面试倒计时
    }

    // 重置当前题目相关的状态
    resetCurrentQuestionState() {
        console.log('重置当前题目状态');

        // 题目倒计时已移除

        // 停止当前题目录音（如果正在进行）
        if (this.currentQuestionRecorder && this.currentQuestionRecorder.state === 'recording') {
            this.currentQuestionRecorder.stop();
        }

        // 停止音量检测
        this.stopVolumeMonitoring();

        // 重置当前题缓存
        this.currentQuestionChunks = [];

        // 重置静音检测状态
        this.silenceDuration = 0;
        this.lastSoundTime = 0;
        if (this.silenceTimeout) {
            clearTimeout(this.silenceTimeout);
            this.silenceTimeout = null;
        }

        // 重置转录相关状态
        this.transcriptBuffer = '';
        this.intermediateText = '';
        this.transcribedText = '';
        this.accumulatedText = '';
        
        // 清空转写文本，但保持容器显示
        // 注意：即使不在录音状态，也要显示容器（因为可能即将开始录音）
        const transcriptText = document.getElementById('transcriptText');
        const intermediateTextEl = document.getElementById('intermediateText');
        const emptyTextEl = document.getElementById('emptyText');
        const transcriptionContainer = document.getElementById('transcriptionContainer');
        
        if (transcriptText) transcriptText.textContent = '';
        if (intermediateTextEl) intermediateTextEl.textContent = '';
        
        // 确保容器显示（无论是否在录音）
        if (transcriptionContainer) {
            transcriptionContainer.style.display = 'flex';
        }
        
        // 如果容器是空的，显示空状态提示
        if (emptyTextEl && !transcriptText?.textContent && !intermediateTextEl?.textContent) {
            emptyTextEl.style.display = 'block';
            emptyTextEl.textContent = '等待语音输入...';
        }
        
        if (this.transcriptionInterval) {
            clearInterval(this.transcriptionInterval);
            this.transcriptionInterval = null;
        }

        // 重新初始化当前题目录音（如果全程录音正在进行）
        if (this.fullRecordingRecorder && this.fullRecordingRecorder.state === 'recording' && this.stream) {
            this.startCurrentQuestionRecording(this.stream);
            this.startVolumeMonitoring();
            this.startRealTimeTranscription();
        }

        // RecordRTC 流式面试：切下一题时常不释放麦克风流，但上面已 stopVolumeMonitoring 销毁了 Analyser。
        // 若不重建，第 2 题及以后无波形；麦克风数据仍进 RecordRTC/WebSocket，仅可视化断掉。
        if (this.stream && this.stream.active && (this.recordRTC || this.isRecording)) {
            const rtcOn = this.recordRTC && typeof this.recordRTC.getState === 'function'
                && this.recordRTC.getState() === 'recording';
            if (rtcOn || this.isRecording) {
                this.initAudioAnalysis(this.stream);
            }
        }

        this.syncTranscriptionVisualMode();

        console.log('当前题目状态已重置');
    }

    // 生成并存储当前题目的TTS语音 - 暂时屏蔽
    async generateAndStoreQuestionAudio(questionText) {
        // 暂时屏蔽TTS功能，只显示文本
        console.log('TTS功能已暂时屏蔽，只显示题目文本');
        this.updatePlayQuestionButton(false);
        // 直接启用输入控件
        this.enableInputControls();
    }

    // 更新题目播放按钮状态
    updatePlayQuestionButton(enabled) {
        const playBtn = document.getElementById('playQuestionBtn');
        if (playBtn) {
            playBtn.disabled = !enabled;
            if (enabled) {
                playBtn.title = '播放当前题目语音';
            } else {
                playBtn.title = '题目语音不可用';
            }
        }
    }

    // 获取当前题目的类型信息
    getCurrentQuestionType() {
        if (this.currentQuestionIndex < this.allQuestions.length) {
            return this.allQuestions[this.currentQuestionIndex].type;
        }
        return 'unknown';
    }

    // 获取当前题目在对应类型中的序号
    getCurrentQuestionNumberInType() {
        const currentType = this.getCurrentQuestionType();
        if (currentType === 'unknown') return 0;

        let count = 0;
        for (let i = 0; i <= this.currentQuestionIndex; i++) {
            if (this.allQuestions[i].type === currentType) {
                count++;
            }
        }
        return count;
    }

    // 使用TTS播放问题 - 暂时屏蔽
    async playQuestionByTTS(questionText, questionNumber) {
        // 暂时屏蔽TTS功能
        console.log(`TTS功能已暂时屏蔽，跳过题目 ${questionNumber} 的语音播放`);
    }

    // 获取指定类型题目的总数
    getTotalQuestionsByType(type) {
        return this.allQuestions.filter(item => item.type === type).length;
    }

    // ==================== 音量和静音检测 ====================
    // 初始化Web Audio API用于音量检测 / 波形可视化（与 ASR 音频链独立，仅分析同一麦克风流）
    initAudioAnalysis(stream) {
        this.stopVolumeMonitoring();
        try {
            this.audioContext = new (window.AudioContext || window.webkitAudioContext)();
            this.analyser = this.audioContext.createAnalyser();
            this.analyser.fftSize = 512;
            this.analyser.smoothingTimeConstant = 0.45;

            const source = this.audioContext.createMediaStreamSource(stream);
            source.connect(this.analyser);

            if (this.audioContext.state === 'suspended') {
                this.audioContext.resume().catch(() => {});
            }

            console.log('Web Audio API初始化成功，用于音量检测与波形');
        } catch (error) {
            console.error('Web Audio API初始化失败:', error);
            this.audioContext = null;
            this.analyser = null;
        }
    }

    /** RecordRTC 或新音频系统下是否处于答题录音中（兼容部分移动端 getState 滞后） */
    isAnswerRecordingActive() {
        if (this.useNewAudioSystem && this.audioManager && this.audioManager.isRecording) {
            return true;
        }
        if (this.recordRTC) {
            const st = typeof this.recordRTC.getState === 'function'
                ? this.recordRTC.getState()
                : '';
            if (st === 'recording') {
                return true;
            }
            // 移动端偶发 getState 未及时变为 recording，但已开始采集且麦克风流仍活跃
            if (this.isRecording && this.stream && typeof this.stream.active === 'boolean' && this.stream.active) {
                return true;
            }
        }
        return false;
    }

    stopVolumeVisualizerLoop() {
        this._volumeVisualizerRunning = false;
        if (this._volumeVisualizerRaf != null) {
            cancelAnimationFrame(this._volumeVisualizerRaf);
            this._volumeVisualizerRaf = null;
        }
    }

    startVolumeVisualizerLoop() {
        if (this._volumeVisualizerRunning || !this.analyser) {
            return;
        }
        if (!this.isAnswerRecordingActive()) {
            return;
        }
        const wrap = document.getElementById('volumeVisualizerWrap');
        if (!wrap) {
            return;
        }
        // 必须用计算样式：移动端 CSS 可能写死 display，style 属性为空时仍会隐藏
        if (window.getComputedStyle(wrap).display === 'none') {
            return;
        }

        this._volumeVisualizerRunning = true;
        const tick = () => {
            if (!this._volumeVisualizerRunning) {
                return;
            }
            if (!this.analyser || !this.isAnswerRecordingActive()) {
                this.stopVolumeVisualizerLoop();
                return;
            }

            if (this.audioContext && this.audioContext.state === 'suspended') {
                this.audioContext.resume().catch(() => {});
            }

            const canvas = document.getElementById('volumeVisualizerCanvas');
            if (!canvas) {
                this._volumeVisualizerRaf = requestAnimationFrame(tick);
                return;
            }

            const crect = canvas.getBoundingClientRect();
            const parent = canvas.parentElement;
            const cssW = crect.width || (parent ? parent.clientWidth : 0) || 320;
            const cssH = crect.height || 88;
            const dpr = Math.min(window.devicePixelRatio || 1, 2);
            const w = Math.max(2, Math.floor((cssW || 320) * dpr));
            const h = Math.max(2, Math.floor(cssH * dpr));
            if (canvas.width !== w || canvas.height !== h) {
                canvas.width = w;
                canvas.height = h;
            }

            const barCount = w < 360 * dpr ? 24 : 32;
            const binCount = this.analyser.frequencyBinCount;
            const freq = new Uint8Array(binCount);
            this.analyser.getByteFrequencyData(freq);

            if (!this._volumeBarSmooth || this._volumeBarSmooth.length !== barCount) {
                this._volumeBarSmooth = new Float32Array(barCount);
            }

            // 偏重中低频（人声能量集中区），sqrt 拉伸让小声时也有起伏
            for (let i = 0; i < barCount; i++) {
                const t = i / Math.max(1, barCount - 1);
                const binStart = Math.floor(t * t * (binCount * 0.35));
                const binEnd = Math.min(binCount, Math.ceil((t * 0.92 + 0.08) * (binCount * 0.45)));
                let maxv = 0;
                for (let b = binStart; b < binEnd; b++) {
                    if (freq[b] > maxv) maxv = freq[b];
                }
                const norm = Math.sqrt(maxv / 255);
                const target = Math.min(1, norm * 1.85);
                this._volumeBarSmooth[i] = this._volumeBarSmooth[i] * 0.55 + target * 0.45;
            }

            const ctx = canvas.getContext('2d');
            if (!ctx) {
                this._volumeVisualizerRaf = requestAnimationFrame(tick);
                return;
            }

            ctx.clearRect(0, 0, w, h);
            const mid = h / 2;
            const gap = Math.max(2, Math.round(dpr * 1.2));
            const totalGap = gap * Math.max(0, barCount - 1);
            const barW = Math.max(2, Math.floor((w - totalGap) / barCount));
            const radius = Math.min(barW / 2, 4 * dpr);

            const canRound = typeof ctx.roundRect === 'function';
            const drawRoundedBar = (x, yTop, bw, bh) => {
                if (bh < 1) return;
                if (canRound) {
                    ctx.beginPath();
                    ctx.roundRect(x, yTop, bw, bh, radius);
                    ctx.fill();
                } else {
                    ctx.fillRect(x, yTop, bw, bh);
                }
            };

            ctx.save();
            ctx.shadowColor = 'rgba(99, 102, 241, 0.35)';
            ctx.shadowBlur = 10 * dpr;
            ctx.shadowOffsetY = 2 * dpr;

            const grad = ctx.createLinearGradient(0, 0, 0, h);
            grad.addColorStop(0, 'rgba(196, 181, 253, 0.95)');
            grad.addColorStop(0.45, 'rgba(129, 140, 248, 0.92)');
            grad.addColorStop(1, 'rgba(99, 102, 241, 0.65)');
            ctx.fillStyle = grad;

            for (let i = 0; i < barCount; i++) {
                const amp = this._volumeBarSmooth[i];
                const bh = Math.max(3 * dpr, amp * (h * 0.46));
                const x = Math.floor(i * (barW + gap));
                drawRoundedBar(x, mid - bh / 2, barW, bh);
            }
            ctx.restore();

            // 中心细线（装饰）
            ctx.strokeStyle = 'rgba(148, 163, 184, 0.25)';
            ctx.lineWidth = dpr;
            ctx.beginPath();
            ctx.moveTo(0, mid);
            ctx.lineTo(w, mid);
            ctx.stroke();

            this._volumeVisualizerRaf = requestAnimationFrame(tick);
        };

        this._volumeVisualizerRaf = requestAnimationFrame(tick);
    }

    /** 根据 showAsrText 与录音状态切换转写区 / 波形区（不改变内存中的转写数据） */
    syncTranscriptionVisualMode() {
        const panel = document.getElementById('transcriptionTextPanel');
        const wrap = document.getElementById('volumeVisualizerWrap');
        if (!panel || !wrap) {
            return;
        }

        const recording = this.isAnswerRecordingActive();

        if (this.showAsrText) {
            wrap.classList.remove('volume-visualizer-wrap--visible');
            wrap.style.removeProperty('display');
            wrap.setAttribute('aria-hidden', 'true');
            panel.style.display = '';
            panel.removeAttribute('aria-hidden');
            this.stopVolumeVisualizerLoop();
            return;
        }

        panel.style.display = 'none';
        panel.setAttribute('aria-hidden', 'true');

        if (recording && this.analyser) {
            wrap.classList.add('volume-visualizer-wrap--visible');
            wrap.style.setProperty('display', 'flex', 'important');
            wrap.setAttribute('aria-hidden', 'false');
            if (this.audioContext && this.audioContext.state === 'suspended') {
                this.audioContext.resume().catch(() => {});
            }
            this.startVolumeVisualizerLoop();
        } else {
            wrap.classList.remove('volume-visualizer-wrap--visible');
            wrap.style.removeProperty('display');
            wrap.setAttribute('aria-hidden', 'true');
            this.stopVolumeVisualizerLoop();
        }
    }

    /** 录音刚开始后移动端布局/RecordRTC 状态可能晚一帧就绪，补几次同步保证波形区显示 */
    syncTranscriptionVisualModeAfterRecordStart() {
        this.syncTranscriptionVisualMode();
        requestAnimationFrame(() => this.syncTranscriptionVisualMode());
        setTimeout(() => this.syncTranscriptionVisualMode(), 50);
        setTimeout(() => this.syncTranscriptionVisualMode(), 200);
    }

    async loadInterviewUiConfig() {
        try {
            // 优先使用服务端在 HTML 中注入的值（与 config.yaml 一致，NATAPP/旧版无 ui-config 接口时仍生效）
            const injected = typeof window !== 'undefined' ? window.__INTERVIEW_UI__ : undefined;
            if (injected && typeof injected.show_asr_text === 'boolean') {
                this.showAsrText = injected.show_asr_text;
                this.syncTranscriptionVisualMode();
            }

            if (!window.API || !window.API.interview
                || typeof window.API.interview.getInterviewUiConfig !== 'function') {
                return;
            }
            const data = await window.API.interview.getInterviewUiConfig();
            if (data && typeof data.show_asr_text === 'boolean') {
                this.showAsrText = data.show_asr_text;
            }
            this.syncTranscriptionVisualMode();
        } catch (e) {
            console.warn('面试 UI 配置 API 失败（若已注入 __INTERVIEW_UI__ 则仍以注入为准）', e);
        }
    }

    // 获取当前音频音量（分贝值）
    getCurrentVolume() {
        if (!this.analyser) return -Infinity;

        const bufferLength = this.analyser.frequencyBinCount;
        const dataArray = new Uint8Array(bufferLength);
        this.analyser.getByteFrequencyData(dataArray);

        // 计算RMS（均方根）值
        let sum = 0;
        for (let i = 0; i < bufferLength; i++) {
            sum += dataArray[i] * dataArray[i];
        }
        const rms = Math.sqrt(sum / bufferLength);

        // 转换为分贝值
        const db = rms > 0 ? 20 * Math.log10(rms / 128) : -Infinity;

        return db;
    }

    // 开始音量检测和静音监控
    startVolumeMonitoring() {
        if (!this.analyser) {
            console.warn('音频分析器未初始化，无法进行音量检测');
            return;
        }

        let vadActiveFrames = 0;
        let totalFrames = 0;

        this.volumeCheckInterval = setInterval(() => {
            const currentVolume = this.getCurrentVolume();
            const now = Date.now();
            totalFrames++;

            // 更新音频质量指标
            if (this.audioQualityMetrics) {
                this.audioQualityMetrics.totalSamples = totalFrames;
            }

            // VAD检测：判断是否有声音活动
            const isVoiceActive = currentVolume > this.silenceThreshold;
            if (isVoiceActive) {
                vadActiveFrames++;
            }

            // 计算VAD活动百分比
            const vadActivityPercent = totalFrames > 0 ? (vadActiveFrames / totalFrames * 100) : 0;
            if (this.audioQualityMetrics) {
                this.audioQualityMetrics.vadActivity = Math.round(vadActivityPercent);
            }

            // 每秒输出一次音量信息（减少日志噪音）
            if (totalFrames % 10 === 0) {
                console.log(`音量: ${currentVolume.toFixed(2)} dB, VAD: ${vadActivityPercent.toFixed(1)}%`);
            }

            // 判断是否有声音
            if (isVoiceActive) {
                // 检测到声音
                this.lastSoundTime = now;
                this.silenceDuration = 0;

                // 重置静音计时器
                if (this.silenceTimeout) {
                    clearTimeout(this.silenceTimeout);
                    this.silenceTimeout = null;
                }
            } else {
                // 当前是静音
                this.silenceDuration = now - this.lastSoundTime;

                // 如果静音持续3秒，自动提交答案
                if (this.silenceDuration >= 3000 && !this.silenceTimeout) {
                    console.log('检测到3秒静音，准备自动提交答案');
                    this.silenceTimeout = setTimeout(() => {
                        this.autoSubmitCurrentQuestion();
                    }, 0); // 立即触发，因为已经等待了3秒
                }
            }

        }, 100); // 每100ms检测一次

        console.log('音量检测已启动，静音阈值:', this.silenceThreshold, 'dB');
    }

    // 停止音量检测
    stopVolumeMonitoring() {
        this.stopVolumeVisualizerLoop();

        if (this.volumeCheckInterval) {
            clearInterval(this.volumeCheckInterval);
            this.volumeCheckInterval = null;
        }

        if (this.audioContext && this.audioContext.state !== 'closed') {
            this.audioContext.close();
            this.audioContext = null;
        }

        this.analyser = null;
        console.log('音量检测已停止');
    }

    // ==================== 双缓存录音系统 ====================
    // 回退到原生MediaRecorder
    fallbackToNativeRecorder(stream, type) {
        const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus') ?
            'audio/webm;codecs=opus' : 'audio/webm';

        if (type === 'fullRecording') {
            this.fullRecordingRecorder = new MediaRecorder(stream, { mimeType: mimeType });
            this.setupRecordingHandlers(this.fullRecordingRecorder, 'fullRecording');
        } else if (type === 'currentQuestion') {
            this.currentQuestionRecorder = new MediaRecorder(stream, { mimeType: mimeType });
            this.setupRecordingHandlers(this.currentQuestionRecorder, 'currentQuestion');
        }

        console.log(`ℹ️ 使用原生MediaRecorder进行${type === 'fullRecording' ? '全程' : '当前题目'}录音`);
    }

    // 设置录音器的事件处理器
    setupRecordingHandlers(recorder, type) {
        recorder.ondataavailable = (event) => {
            if (event.data.size > 0) {
                if (type === 'fullRecording') {
                    this.fullRecordingChunks.push(event.data);
                    console.log(`全程录音块已添加，大小: ${event.data.size} bytes`);
                } else if (type === 'currentQuestion') {
                    this.currentQuestionChunks.push(event.data);
                    console.log(`当前题录音块已添加，大小: ${event.data.size} bytes`);
                }
            }
        };

        recorder.onstop = () => {
            console.log(`${type === 'fullRecording' ? '全程' : '当前题目'}录音已停止`);
        };
    }

    // 开始全程录音
    async startFullRecording(stream) {
        try {
            // 检查OpusMediaRecorder是否可用
            if (typeof OpusMediaRecorder !== 'undefined' && window.OpusMediaRecorderAvailable === true) {
                try {
                    // 使用OpusMediaRecorder确保完整的WebM容器头
                    this.fullRecordingRecorder = new OpusMediaRecorder(stream, {
                        mimeType: 'audio/webm;codecs=opus'
                    });
                    this.setupRecordingHandlers(this.fullRecordingRecorder, 'fullRecording');
                    console.log('✅ 使用OpusMediaRecorder进行全程录音（高质量模式）');
                } catch (error) {
                    console.warn('⚠️ OpusMediaRecorder初始化失败，回退到原生MediaRecorder:', error.message);
                    console.warn('⚠️ 可能的原因：缺少WebAssembly文件(WebMOpusEncoder.wasm)');
                    this.fallbackToNativeRecorder(stream, 'fullRecording');
                }
            } else {
                // 回退到原生MediaRecorder
                const reason = window.OpusMediaRecorderAvailable === false ?
                    '库加载失败' : '库不可用';
                console.log(`ℹ️ OpusMediaRecorder${reason}，使用原生MediaRecorder进行全程录音（兼容模式）`);
                this.fallbackToNativeRecorder(stream, 'fullRecording');
            }

            this.fullRecordingChunks = [];

            this.setupRecordingHandlers(this.fullRecordingRecorder, 'fullRecording');

            this.fullRecordingRecorder.start(1000); // 每秒收集一次数据
            console.log('全程录音已启动');
        } catch (error) {
            console.error('启动全程录音失败:', error);
            throw error;
        }
    }

    // 开始当前题目录音
    async startCurrentQuestionRecording(stream) {
        try {
            // 检查OpusMediaRecorder是否可用
            if (typeof OpusMediaRecorder !== 'undefined' && window.OpusMediaRecorderAvailable === true) {
                try {
                    // 使用OpusMediaRecorder确保完整的WebM容器头
                    this.currentQuestionRecorder = new OpusMediaRecorder(stream, {
                        mimeType: 'audio/webm;codecs=opus'
                    });
                    this.setupRecordingHandlers(this.currentQuestionRecorder, 'currentQuestion');
                    console.log('✅ 使用OpusMediaRecorder进行当前题目录音（高质量模式）');
                } catch (error) {
                    console.warn('⚠️ OpusMediaRecorder初始化失败，回退到原生MediaRecorder:', error.message);
                    console.warn('⚠️ 可能的原因：缺少WebAssembly文件(WebMOpusEncoder.wasm)');
                    this.fallbackToNativeRecorder(stream, 'currentQuestion');
                }
            } else {
                // 回退到原生MediaRecorder
                const reason = window.OpusMediaRecorderAvailable === false ?
                    '库加载失败' : '库不可用';
                console.log(`ℹ️ OpusMediaRecorder${reason}，使用原生MediaRecorder进行当前题目录音（兼容模式）`);
                this.fallbackToNativeRecorder(stream, 'currentQuestion');
            }

            // 核心修改：使用统一的audioChunks数组积累音频数据
            this.audioChunks = [];
            this.currentMimeType = 'audio/webm;codecs=opus'; // 记录MIME类型

            this.currentQuestionRecorder.ondataavailable = (event) => {
                if (event.data.size > 0) {
                    this.audioChunks.push(event.data);
                    const totalSize = this.audioChunks.reduce((sum, chunk) => sum + chunk.size, 0);
                    console.log(`🎵 音频块已积累: +${event.data.size} bytes，累计: ${totalSize} bytes (${this.audioChunks.length}个块)`);
                } else {
                    console.warn('⚠️ 收到空的音频数据块');
                }
            };

            this.currentQuestionRecorder.onstop = () => {
                console.log('🎤 当前题目录音已停止');

                // 核心逻辑：录音停止后，合并所有音频块为完整Blob并自动发送
                if (this.audioChunks.length > 0) {
                    const totalSize = this.audioChunks.reduce((sum, chunk) => sum + chunk.size, 0);
                    const duration = totalSize / (16000 * 2); // 估算时长（16kHz, 16bit单声道）

                    console.log(`📊 录音完成: ${this.audioChunks.length}个音频块，${totalSize} bytes，约${duration.toFixed(2)}秒`);

                    // 合并所有音频块为完整Blob
                    const completeAudioBlob = new Blob(this.audioChunks, { type: this.currentMimeType });
                    console.log(`🔄 已合并为完整音频Blob: ${completeAudioBlob.size} bytes`);

                    // 自动发送完整音频进行识别
                    this.processCompleteAudio(completeAudioBlob).catch(err => {
                        console.error('❌ 发送完整音频失败:', err);
                        this.showError('语音识别失败，请重试');
                    });

                } else {
                    console.warn('⚠️ 没有录制到音频数据');
                    this.showError('没有录制到音频，请重试');
                }
            };

            // 设置合理的timeslice，确保数据积累
            this.currentQuestionRecorder.start(); // 完整录制模式，录制到停止为止
            console.log('🎤 当前题目录音已启动（完整录制模式）');
        } catch (error) {
            console.error('启动当前题目录音失败:', error);
            throw error;
        }
    }

    // 停止当前题目录音并返回音频Blob
    async stopCurrentQuestionRecording() {
        return new Promise((resolve) => {
            if (!this.currentQuestionRecorder) {
                resolve(null);
                return;
            }

            this.currentQuestionRecorder.onstop = () => {
                if (this.currentQuestionChunks.length > 0) {
                    const audioBlob = new Blob(this.currentQuestionChunks, {
                        type: 'audio/webm;codecs=opus'
                    });
                    console.log(`当前题目录音已完成，Blob大小: ${audioBlob.size} bytes`);
                    resolve(audioBlob);
                } else {
                    console.log('当前题目没有录制到音频');
                    resolve(null);
                }
            };

            this.currentQuestionRecorder.stop();
        });
    }

    // 停止全程录音并返回完整音频Blob
    async stopFullRecording() {
        return new Promise((resolve) => {
            if (!this.fullRecordingRecorder) {
                resolve(null);
                return;
            }

            this.fullRecordingRecorder.onstop = () => {
                if (this.fullRecordingChunks.length > 0) {
                    const audioBlob = new Blob(this.fullRecordingChunks, {
                        type: 'audio/webm;codecs=opus'
                    });
                    console.log(`全程录音已完成，Blob大小: ${audioBlob.size} bytes`);
                    resolve(audioBlob);
                } else {
                    console.log('全程录音为空');
                    resolve(null);
                }
            };

            this.fullRecordingRecorder.stop();
        });
    }

    // 自动提交当前题目答案
    async autoSubmitCurrentQuestion() {
        console.log('=== 自动提交当前题目答案 ===');

        // 停止当前题目录音
        const questionAudioBlob = await this.stopCurrentQuestionRecording();

        if (questionAudioBlob) {
            // 提交当前题目音频
            await this.submitQuestionAudio(questionAudioBlob);
        } else {
            console.warn('没有录制到当前题目音频');
        }

        // 停止音量检测
        this.stopVolumeMonitoring();

        // 更新UI状态
        this.updateButtonStates('active');

        // 清除静音计时器
        if (this.silenceTimeout) {
            clearTimeout(this.silenceTimeout);
            this.silenceTimeout = null;
        }
    }

    // 提交题目音频到后端
    async submitQuestionAudio(audioBlob) {
        try {
            console.log('正在提交题目音频，大小:', audioBlob.size, 'bytes');

            // 转换为Base64
            const reader = new FileReader();
            reader.onloadend = async () => {
                const base64Audio = reader.result.split(',')[1];

                try {
                    const result = await window.API.interview.submitVoiceAnswer(
                        this.currentSession.session_id,
                        base64Audio
                    );

                    if (result.success) {
                        console.log('题目音频提交成功');

                        // 显示用户回答（ASR结果）
                        const answerText = result.data.asr_result?.text || '语音回答';
                        this.addUserMessage(answerText);

                        // 执行LLM评分
                        await this.performLLMEvaluation(answerText);

                        // 获取下一问题或完成面试
                        await this.handleAnswerSubmission(answerText);
                    } else {
                        console.error('题目音频提交失败:', result.message);
                        this.showError('提交音频失败: ' + result.message);
                    }
                } catch (error) {
                    console.error('提交题目音频时出错:', error);
                    this.showError('提交音频失败: ' + error.message);
                }
            };

            reader.readAsDataURL(audioBlob);
        } catch (error) {
            console.error('准备提交题目音频失败:', error);
        }
    }

    // 发送权限状态到后端
    sendPermissionStatusToBackend(status, message) {
        try {
            // 创建WebSocket连接到语音面试服务器
            // 使用与HTTP API相同的端口，WebSocket端点为 /ws/asr
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const hostname = window.location.hostname;
            const port = window.location.port || (protocol === 'wss:' ? '443' : '80');
            const wsUrl = `${protocol}//${hostname}:${port}/ws/asr`;

            const ws = new WebSocket(wsUrl);

            ws.onopen = () => {
                console.log('✅ 权限状态WebSocket连接成功');

                // 发送权限状态消息
                const permissionMessage = {
                    type: 'permission_status',
                    state: status,
                    message: message,
                    timestamp: new Date().toISOString(),
                    userAgent: navigator.userAgent
                };

                try {
                    ws.send(JSON.stringify(permissionMessage));
                    console.log('📤 已发送权限状态到后端:', permissionMessage);

                    // 等待消息发送完成后再关闭连接（给一点时间确保消息发送）
                    setTimeout(() => {
                        if (ws.readyState === WebSocket.OPEN) {
                            ws.close();
                        }
                    }, 200);
                } catch (error) {
                    console.warn('⚠️ 发送权限状态消息失败:', error);
                    ws.close();
                }
            };

            ws.onerror = (error) => {
                console.warn('⚠️ 权限状态WebSocket连接失败:', error);
                // 不影响主要功能，只是无法显示后端权限状态
            };

            ws.onclose = () => {
                console.log('🔌 权限状态WebSocket连接已关闭');
            };

        } catch (error) {
            console.warn('⚠️ 发送权限状态到后端失败:', error);
            // 不影响主要功能
        }
    }

    // ==================== 音频录制相关 ====================
    async startRecording() {
        try {
            if (!this.currentQuestion) {
                this.showError('请先获取问题');
                return;
            }

            this.showLoading('准备录制', '正在初始化录音设备');

            // 在录音前再次检查麦克风权限
            console.log('录音前检查麦克风权限...');
            if (navigator.permissions) {
                try {
                    const permissionStatus = await navigator.permissions.query({ name: 'microphone' });
                    if (permissionStatus.state === 'denied') {
                        this.hideLoading();
                        this.showError('麦克风权限已被拒绝，无法开始录音。请在浏览器设置中允许访问麦克风。');
                        return;
                    }
                } catch (error) {
                    console.warn('无法检查权限状态，继续尝试录音:', error);
                }
            }

            // 检查浏览器支持
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                this.hideLoading();
                this.showError('您的浏览器不支持麦克风访问功能。建议使用Chrome、Firefox、Safari等现代浏览器，并确保页面通过HTTPS访问。');
                return;
            }

            // 获取音频流（优化移动设备兼容性）
            const isMobile = /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);
            const audioConstraints = isMobile ? {
                audio: {
                    echoCancellation: true,
                    noiseSuppression: true,
                    autoGainControl: true,
                    sampleRate: 44100,
                    channelCount: 1
                }
            } : { audio: true };

            const stream = await navigator.mediaDevices.getUserMedia(audioConstraints);
            
            // 选择最佳录音格式并创建MediaRecorder
            const mimeType = this.selectBestMimeType();
            if (!mimeType) {
                throw new Error('浏览器不支持任何音频录制格式，请升级浏览器');
            }

            const recorderOptions = { mimeType: mimeType };
            this.mediaRecorder = new MediaRecorder(stream, recorderOptions);

            // 保存实际使用的MIME类型，用于后续创建Blob
            this.currentMimeType = mimeType;
            
            this.audioChunks = [];
            
            this.mediaRecorder.ondataavailable = (event) => {
                if (event.data.size > 0) {
                    this.audioChunks.push(event.data);
                }
            };
            
            this.mediaRecorder.onstop = () => {
                // 停止所有音频轨道
                stream.getTracks().forEach(track => track.stop());
            };
            
            this.mediaRecorder.start();
            
            // 更新UI状态
            document.getElementById('startRecordingBtn').disabled = true;
            document.getElementById('stopRecordingBtn').disabled = false;
            document.getElementById('recordingIndicator').classList.add('active');
            document.getElementById('textAnswerInput').classList.remove('active');
            
            // 开始计时
            this.recordingDuration = 0;
            this.recordingTimer = setInterval(() => {
                this.recordingDuration++;
            }, 1000);
            
            this.hideLoading();
            
        } catch (error) {
            console.error('开始录制失败:', error);
            this.hideLoading();
            this.showError('开始录制失败: ' + error.message);
        }
    }

    async stopRecording() {
        try {
            if (!this.mediaRecorder || this.mediaRecorder.state !== 'recording') {
                return;
            }
            
            this.mediaRecorder.stop();
            
            // 更新UI状态
            const voiceRecordBtn = document.getElementById('voiceRecordBtn');
            const recordingStatus = document.getElementById('recordingStatus');

            if (voiceRecordBtn) {
                voiceRecordBtn.classList.remove('recording');
            }
            if (recordingStatus) {
                recordingStatus.style.display = 'none';
            }
            
            // 清除计时器
            if (this.recordingTimer) {
                clearInterval(this.recordingTimer);
                this.recordingTimer = null;
            }
            
            // 等待录制停止
            setTimeout(async () => {
                await this.submitVoiceAnswer();
            }, 500);
            
        } catch (error) {
            console.error('停止录制失败:', error);
            this.showError('停止录制失败');
        }
    }

    async submitVoiceAnswer() {
        try {
            if (this.audioChunks.length === 0) {
                this.showError('没有录制到音频');
                return;
            }
            
            this.showLoading('处理回答', '正在处理语音回答');
            
            // 创建音频Blob，使用实际录制的格式
            const audioBlob = new Blob(this.audioChunks, { type: this.currentMimeType || 'audio/webm' });
            
            // 转换为Base64
            const reader = new FileReader();
            reader.onloadend = async () => {
                const base64Audio = reader.result.split(',')[1];
                
                try {
                    // 提交语音回答
                    const result = await window.API.interview.submitVoiceAnswer(
                        this.currentSession.session_id,
                        base64Audio
                    );
                    
                    if (result.success) {
                        // 显示用户回答（ASR结果）
                        const answerText = result.data.asr_result?.text || '语音回答';
                        this.addUserMessage(answerText);
                        
                        if (result.data.is_completed) {
                            // 面试完成
                            this.updateStatus('已完成', 'completed');
                            this.updateButtonStates('completed');
                            this.addBotMessage('面试完成！感谢您的参与。');
                            this.showSuccess('面试完成！感谢您的参与');
                        } else {
                            // 获取下一问题
                            if (result.data.next_question) {
                                this.updateCurrentQuestion(result.data.next_question);
                                this.updateProgress(result.data.current_index, result.data.total_questions);

                                // 暂时屏蔽语音播放，直接启用输入控件
                                console.log('语音播放功能已暂时屏蔽，直接启用文本输入');
                                this.enableInputControls();
                            }
                        }
                    }
                    
                    this.hideLoading();
                    
                } catch (error) {
                    console.error('提交语音回答失败:', error);
                    this.hideLoading();
                    this.showError('提交语音回答失败: ' + error.message);
                }
            };
            
            reader.readAsDataURL(audioBlob);
            
        } catch (error) {
            console.error('提交语音回答失败:', error);
            this.hideLoading();
            this.showError('提交语音回答失败: ' + error.message);
        }
    }

    // 文本回答功能已删除，只支持语音回答
    
    // 发送消息（统一入口）- 现在只支持语音录制
    async sendMessage() {
        // 文本输入功能已删除，只支持语音录制
        console.log('只支持语音录制，不支持文本发送');
    }

    // ==================== UI更新方法 ====================
    updateStatus(status, statusType) {
        // 由于HTML结构重组，statusIndicator已不存在，只更新statusText（如果存在）
        const statusText = document.getElementById('statusText');
        if (statusText) {
            statusText.textContent = status;
        }

        // 状态颜色现在通过CSS类或其他方式处理，不再需要手动设置背景色
        console.log(`状态更新: ${status} (${statusType})`);
    }

    updateSessionId(sessionId) {
        // 会话ID不再在界面上显示，但保留功能
        this.currentSessionId = sessionId;
    }

    updateProgress(current, total) {
        const progressFill = document.getElementById('progressFill');
        const progressPercent = document.getElementById('progressPercent');
        const questionCounter = document.querySelector('.question-counter');

        const percentage = total > 0 ? (current / total) * 100 : 0;

        if (progressFill) {
            progressFill.style.width = `${percentage}%`;
        }

        if (progressPercent) {
            progressPercent.textContent = `${Math.round(percentage)}%`;
        }

        // 更新新的进度条显示
        if (questionCounter) {
            // 使用currentQuestionIndex + 1作为当前题号（因为索引从0开始）
            const currentNumber = current;  // 直接使用传入的current参数
            questionCounter.textContent = `第 ${currentNumber} / ${total} 题`;
            console.log(`📊 更新题目计数器: ${currentNumber}/${total}`);
        }

        // 更新Element Plus进度条
        const elProgress = document.querySelector('.interview-progress-bar el-progress');
        if (elProgress && typeof elProgress.setAttribute === 'function') {
            elProgress.setAttribute('percentage', Math.round(percentage));
            if (percentage === 100) {
                elProgress.setAttribute('status', 'success');
            } else {
                elProgress.setAttribute('status', 'normal');
            }
        }

        // 更新详细进度信息（如果有题目类型信息）
        this.updateDetailedProgress(current);
    }

    updateDetailedProgress(currentQuestionNumber = null) {
        // 如果没有传入当前问题序号，使用内部的currentQuestionIndex + 1
        const current = currentQuestionNumber || (this.currentQuestionIndex + 1);
        const totalBasic = this.getTotalQuestionsByType('basic');
        const totalProfessional = this.getTotalQuestionsByType('professional');

        // 计算基础题和专业题的已完成数量
        // current是从1开始的问题序号
        const basicCompleted = current <= totalBasic ? current : totalBasic;
        const professionalCompleted = current > totalBasic ? Math.min(current - totalBasic, totalProfessional) : 0;

        // 计算百分比
        const percentage = this.totalQuestions > 0 ? Math.round((current / this.totalQuestions) * 100) : 0;

        // 更新进度文本显示，同时显示总题数、百分比和基础题、专业题的进度
        const progressText = document.getElementById('progressText');
        const progressPercent = document.getElementById('progressPercent');
        
        if (progressText) {
            const totalCompleted = Math.min(current, this.totalQuestions);
            // 把百分比合并到总题数那一行
            const totalText = `总题数：${totalCompleted}/${this.totalQuestions} (${percentage}%)`;
            const basicText = totalBasic > 0 ? `基本题 ${basicCompleted}/${totalBasic}` : '';
            const professionalText = totalProfessional > 0 ? `专业题 ${professionalCompleted}/${totalProfessional}` : '';

            // 组合显示：总题数(百分比) + 详细分类
            let displayText = totalText;
            const detailParts = [basicText, professionalText].filter(text => text);
            if (detailParts.length > 0) {
                displayText += ' - ' + detailParts.join(', ');
            }

            progressText.textContent = displayText;
        }
        
        // 隐藏单独的百分比显示（因为已经合并到总题数那一行了）
        if (progressPercent) {
            progressPercent.style.display = 'none';
        }

        // 可以在这里添加更多详细的进度显示逻辑
        console.log(`当前进度: ${current}/${this.totalQuestions} (${percentage}%) (基本题 ${basicCompleted}/${totalBasic}, 专业题 ${professionalCompleted}/${totalProfessional})`);
    }


    updateButtonStates(status) {
        const pauseBtn = document.getElementById('pauseInterviewBtn');
        const resumeBtn = document.getElementById('resumeInterviewBtn');
        const startAnswerBtn = document.getElementById('startAnswerBtn');
        const endInterviewBtn = document.getElementById('endInterviewBtn');

        switch (status) {
            case 'active':
                // 开始面试按钮已移除，直接设置其他按钮状态
                if (pauseBtn) pauseBtn.disabled = false;
                if (resumeBtn) resumeBtn.disabled = true;
                if (startAnswerBtn) {
                    startAnswerBtn.disabled = false;
                    // 重置按钮文字和图标
                    const textSpan = startAnswerBtn.querySelector('span');
                    if (textSpan) {
                        textSpan.textContent = '开始回答';
                    }
                    const icon = startAnswerBtn.querySelector('i');
                    if (icon) {
                        icon.className = 'fas fa-microphone';
                    }
                }
                if (endInterviewBtn) endInterviewBtn.disabled = false;
                this.updateRecordingStatus('ready');
                break;
            case 'paused':
                if (pauseBtn) pauseBtn.disabled = true;
                if (resumeBtn) resumeBtn.disabled = false;
                if (startAnswerBtn) startAnswerBtn.disabled = true;
                if (endInterviewBtn) endInterviewBtn.disabled = false;
                this.updateRecordingStatus('paused');
                break;
            case 'completed':
                if (pauseBtn) pauseBtn.disabled = true;
                if (resumeBtn) resumeBtn.disabled = true;
                if (startAnswerBtn) startAnswerBtn.disabled = true;
                if (endInterviewBtn) endInterviewBtn.disabled = true;
                this.updateRecordingStatus('completed');
                break;
            case 'recording':
                if (startAnswerBtn) {
                    startAnswerBtn.disabled = false;  // 录音中保持可点，用于“回答完毕，下一题”（与旧代码一致）
                    const textSpan = startAnswerBtn.querySelector('span');
                    if (textSpan) {
                        textSpan.textContent = '回答完毕，下一题';
                    }
                    const icon = startAnswerBtn.querySelector('i');
                    if (icon) {
                        icon.className = 'fas fa-forward';
                    }
                }
                this.updateRecordingStatus('recording');
                break;
            default:
                // 开始面试按钮已移除，默认状态下禁用其他按钮
                if (pauseBtn) pauseBtn.disabled = true;
                if (resumeBtn) resumeBtn.disabled = true;
                if (startAnswerBtn) startAnswerBtn.disabled = true;
                if (endInterviewBtn) endInterviewBtn.disabled = true;
                this.updateRecordingStatus('idle');
        }
    }

    addToConversationHistory(question, answer, type) {
        const historyContainer = document.getElementById('conversationHistory');
        
        // 清除空状态提示
        const emptyMessage = historyContainer.querySelector('p');
        if (emptyMessage) {
            emptyMessage.remove();
        }
        
        const questionDiv = document.createElement('div');
        questionDiv.className = 'conversation-item question';
        questionDiv.innerHTML = `
            <div class="conversation-text">${question}</div>
            <div class="conversation-meta">问题</div>
        `;
        
        const answerDiv = document.createElement('div');
        answerDiv.className = 'conversation-item answer';
        answerDiv.innerHTML = `
            <div class="conversation-text">${answer}</div>
            <div class="conversation-meta">${type === 'voice' ? '语音回答' : '文本回答'}</div>
        `;
        
        historyContainer.appendChild(questionDiv);
        historyContainer.appendChild(answerDiv);
        
        // 滚动到底部
        historyContainer.scrollTop = historyContainer.scrollHeight;
    }

    // ==================== 模态框相关 ====================
    showLoading(title, description) {
        const modal = document.getElementById('loadingModal');
        document.getElementById('loadingText').textContent = title;
        document.getElementById('loadingDescription').textContent = description;
        modal.style.display = 'flex';
    }

    hideLoading() {
        document.getElementById('loadingModal').style.display = 'none';
    }

    showSuccess(message) {
        const modal = document.getElementById('successModal');
        document.getElementById('successMessage').textContent = message;
        modal.style.display = 'flex';
        
        // 3秒后自动关闭
        setTimeout(() => {
            this.hideSuccessModal();
        }, 3000);
    }

    hideSuccessModal() {
        document.getElementById('successModal').style.display = 'none';
    }

    showError(message) {
        const modal = document.getElementById('errorModal');
        document.getElementById('errorMessage').textContent = message;
        modal.style.display = 'flex';
    }

    hideErrorModal() {
        document.getElementById('errorModal').style.display = 'none';
    }

    showWarning(message) {
        // 使用 showError 的样式，但可以自定义警告样式
        const modal = document.getElementById('errorModal');
        const errorMessage = document.getElementById('errorMessage');
        if (errorMessage) {
            errorMessage.textContent = message;
            errorMessage.style.color = '#ff9800'; // 橙色警告
        }
        if (modal) {
            modal.style.display = 'flex';
            // 3秒后自动关闭
            setTimeout(() => {
                this.hideErrorModal();
                if (errorMessage) {
                    errorMessage.style.color = ''; // 恢复默认颜色
                }
            }, 3000);
        }
    }

    hideAllModals() {
        document.getElementById('loadingModal').style.display = 'none';
        document.getElementById('successModal').style.display = 'none';
        document.getElementById('errorModal').style.display = 'none';
    }

    // ==================== UI控件控制 ====================

    // 检查浏览器音频录制支持情况
    checkAudioRecordingSupport() {
        const support = {
            mediaRecorder: !!window.MediaRecorder,
            getUserMedia: !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia),
            audioContext: !!window.AudioContext || !!window.webkitAudioContext,
            formats: {},
            constraints: {},
            recommended: null
        };

        // 检查各种音频格式支持
        const formats = [
            'audio/wav',
            'audio/wav;codecs=pcm',
            'audio/webm',
            'audio/webm;codecs=opus',
            'audio/webm;codecs=vorbis',
            'audio/mp4',
            'audio/mp4;codecs=mp4a.40.2',
            'audio/ogg;codecs=opus',
            'audio/ogg;codecs=vorbis'
        ];

        formats.forEach(format => {
            if (MediaRecorder.isTypeSupported) {
                support.formats[format] = MediaRecorder.isTypeSupported(format);
            } else {
                support.formats[format] = false;
            }
        });

        // 检查音频约束支持
        const audioConstraints = [
            'sampleRate', 'channelCount', 'echoCancellation',
            'noiseSuppression', 'autoGainControl', 'volume',
            'sampleSize', 'latency'
        ];

        audioConstraints.forEach(constraint => {
            support.constraints[constraint] = navigator.mediaDevices &&
                navigator.mediaDevices.getSupportedConstraints &&
                navigator.mediaDevices.getSupportedConstraints()[constraint];
        });

        // 确定推荐格式（实时转录模式优先使用WAV，确保兼容性）
        // 注意：WAV格式虽然文件较大，但无需后端转换，兼容性最好
        const preferredOrder = this.isRealTimeMode ?
            [
                'audio/wav',                    // 最高优先级：无需转换，兼容性最好
                'audio/wav;codecs=pcm',        // WAV PCM变体
                'audio/webm;codecs=opus',      // WebM-Opus：如果WAV不支持，使用WebM
                'audio/webm;codecs=vorbis',    // WebM-Vorbis：备选
                'audio/webm'                   // 通用WebM
            ] :
            [
                'audio/wav',                   // 普通录音也优先WAV
                'audio/webm;codecs=opus',      // WebM-Opus：质量好
                'audio/webm;codecs=vorbis',    // WebM-Vorbis：备选
                'audio/mp4;codecs=mp4a.40.2',  // MP4-AAC：兼容性好
                'audio/ogg;codecs=opus',       // OGG-Opus：备选
                'audio/webm'                   // 通用WebM
            ];

        for (const format of preferredOrder) {
            if (support.formats[format]) {
                support.recommended = format;
                break;
            }
        }

        console.log('音频录制支持详情:', {
            MediaRecorder: support.mediaRecorder,
            getUserMedia: support.getUserMedia,
            AudioContext: support.audioContext,
            推荐格式: support.recommended,
            所有格式: support.formats,
            约束支持: support.constraints
        });

        return support;
    }

    // 选择最佳的录音MIME类型（基于兼容性和质量）
    selectBestMimeType() {
        console.log('音频格式选择：基于浏览器支持情况选择最佳格式');

        // 获取音频支持信息
        const audioSupport = this.checkAudioRecordingSupport();

        // 首选格式优先级（实时转录优先兼容格式，确保后端能处理）
        const preferredFormats = this.isRealTimeMode ? [
            'audio/webm;codecs=pcm',        // WebM-PCM：兼容性最好，后端能处理
            'audio/wav',                    // WAV：如果支持，无需转换
            'audio/wav;codecs=pcm',         // WAV PCM变体
            'audio/webm;codecs=opus',       // WebM-Opus：备选
            'audio/webm'                    // 通用WebM
        ] : [
            'audio/wav',                    // 普通模式：WAV优先
            'audio/webm;codecs=opus',       // WebM-Opus：质量好，压缩率高
            'audio/mp4;codecs=mp4a.40.2',   // MP4-AAC：兼容性好
            'audio/webm;codecs=vorbis',     // WebM-Vorbis：备选方案
            'audio/ogg;codecs=opus'         // OGG-Opus：备选方案
        ];

        // 从首选格式中选择第一个支持的
        for (const format of preferredFormats) {
            if (audioSupport.formats[format]) {
                console.log(`✓ 选择音频格式: ${format}`);
                return format;
            }
        }

        // 如果没有找到支持的格式，记录错误
        console.error('🚨 无可用音频格式！浏览器MediaRecorder格式支持情况:');
        console.error(audioSupport.formats);
        console.error('建议升级到最新版本的Chrome、Firefox或Safari浏览器');

        return '';
    }

    // 构建Vosk优化音频约束配置（核心优化）
    buildVoskOptimizedConstraints(audioSupport) {
        console.log('🎯 使用Vosk专用音频配置：16kHz单声道');

        // 直接指定Vosk要求的最佳参数，减少后端转换风险
        const constraints = {
            audio: {
                sampleRate: 16000,        // Vosk要求的16kHz采样率
                channelCount: 1,          // 单声道
                sampleSize: 16,           // 16位深度
                echoCancellation: true,   // 回声消除
                noiseSuppression: true,   // 噪音抑制
                autoGainControl: true     // 自动增益控制
            }
        };

        return constraints;
    }

    // 构建最优音频约束配置（兼容旧版）
    buildOptimalAudioConstraints(audioSupport) {
        const constraints = { audio: {} };

        // 采样率：优先16kHz（ASR最优），其次8kHz
        if (audioSupport.constraints.sampleRate) {
            constraints.audio.sampleRate = { ideal: 16000, min: 8000, max: 48000 };
        }

        // 声道数：优先单声道（减少数据量）
        if (audioSupport.constraints.channelCount) {
            constraints.audio.channelCount = { ideal: 1, max: 2 };
        }

        // 音频增强功能
        if (audioSupport.constraints.echoCancellation) {
            constraints.audio.echoCancellation = { ideal: true };
        }
        if (audioSupport.constraints.noiseSuppression) {
            constraints.audio.noiseSuppression = { ideal: true };
        }
        if (audioSupport.constraints.autoGainControl) {
            constraints.audio.autoGainControl = { ideal: true };
        }

        // 音量控制
        if (audioSupport.constraints.volume) {
            constraints.audio.volume = { ideal: 0.8, min: 0.1, max: 1.0 };
        }

        // 采样大小（位深）
        if (audioSupport.constraints.sampleSize) {
            constraints.audio.sampleSize = { ideal: 16, min: 8, max: 32 };
        }

        // 延迟控制
        if (audioSupport.constraints.latency) {
            constraints.audio.latency = { ideal: 0.01, max: 0.1 }; // 10ms理想延迟
        }

        return constraints;
    }

    // 验证音频设置是否符合要求
    validateAudioSettings(settings) {
        const issues = [];

        // 检查采样率
        if (settings.sampleRate < 8000) {
            issues.push(`采样率过低: ${settings.sampleRate}Hz (建议≥8000Hz)`);
        } else if (settings.sampleRate > 48000) {
            issues.push(`采样率过高: ${settings.sampleRate}Hz (建议≤48000Hz)`);
        }

        // 检查声道数
        if (settings.channelCount > 2) {
            issues.push(`声道数过多: ${settings.channelCount}声道 (建议≤2声道)`);
        }

        // 检查延迟
        if (settings.latency > 0.2) {
            console.warn(`音频延迟较高: ${settings.latency}s (可能影响实时性)`);
        }

        if (issues.length > 0) {
            const warningMsg = '音频设置可能影响录制质量:\n' + issues.join('\n');
            console.warn(warningMsg);
            // 不抛出错误，只警告，因为这些设置仍然可以使用
        } else {
            console.log('音频设置验证通过 ✓');
        }

        return issues.length === 0;
    }

    // 校验音频数据完整性
    validateAudioData(audioBlob) {
        return new Promise((resolve, reject) => {
            if (!audioBlob || audioBlob.size === 0) {
                reject(new Error('音频数据为空'));
                return;
            }

            // 检查文件大小（合理范围：1KB - 50MB）
            const minSize = 1024; // 1KB
            const maxSize = 50 * 1024 * 1024; // 50MB
            if (audioBlob.size < minSize) {
                reject(new Error(`音频数据过小: ${audioBlob.size} bytes`));
                return;
            }
            if (audioBlob.size > maxSize) {
                reject(new Error(`音频数据过大: ${audioBlob.size} bytes`));
                return;
            }

            // 读取文件头进行格式校验
            const reader = new FileReader();
            reader.onload = (e) => {
                const buffer = e.target.result;
                const uint8Array = new Uint8Array(buffer.slice(0, 12));
                const header = Array.from(uint8Array).map(b => b.toString(16).padStart(2, '0')).join('');

                console.log(`音频文件头校验: ${header}`);

                // 校验常见音频格式的文件头
                const validHeaders = {
                    // WAV格式
                    '52494646': 'WAV', // RIFF
                    // WebM格式 (增强检测)
                    '1a45dfa3': 'WebM', // EBML 标准头部
                    '1a45dfa4': 'WebM', // EBML 变体
                    // MP3格式
                    '494433': 'MP3', // ID3v2
                    'fffb': 'MP3', // MPEG-1 Layer 3
                    'fffa': 'MP3', // MPEG-1 Layer 3
                    'fff9': 'MP3', // MPEG-2 Layer 3
                    'fffd': 'MP3', // MPEG-2.5
                    // OGG格式
                    '4f676753': 'OGG', // OggS
                    // MP4/fMP4格式 (增强检测)
                    '000000': 'MP4', // 部分MP4
                    '66747970': 'MP4', // ftyp (标准MP4)
                    '4d344120': 'M4A', // M4A (Apple MPEG-4 Audio)
                    '4d344220': 'M4B', // M4B
                    '4d345020': 'MP4', // MP4
                    // AAC格式
                    'fff1': 'AAC', // ADTS
                    'fff9': 'AAC', // ADTS
                    // FLAC格式
                    '664c6143': 'FLAC', // fLaC
                    // WMA格式
                    '3026b275': 'WMA' // Windows Media Audio
                };

                let detectedFormat = null;

                // 第一轮：精确前缀匹配
                for (const [prefix, format] of Object.entries(validHeaders)) {
                    if (header.startsWith(prefix)) {
                        detectedFormat = format;
                        break;
                    }
                }

                // 第二轮：如果没检测到，尝试更宽松的检测
                if (!detectedFormat) {
                    detectedFormat = this._detectAudioFormatAdvanced(uint8Array, header);
                }

                // 第三轮：基于MIME类型和数据特征推断
                if (!detectedFormat) {
                    detectedFormat = this._inferBrowserAudioFormat(uint8Array, audioBlob.type);
                }

                if (detectedFormat) {
                    console.log(`检测到音频格式: ${detectedFormat}`);
                    resolve({
                        valid: true,
                        format: detectedFormat,
                        size: audioBlob.size,
                        header: header
                    });
                } else {
                    console.warn(`未知音频格式，文件头: ${header}, MIME类型: ${audioBlob.type}`);
                    // 对于完全未知的格式，仍然允许后端处理
                    // 后端有更强大的检测和转换能力
                    resolve({
                        valid: true,
                        format: 'unknown',
                        size: audioBlob.size,
                        header: header,
                        mimeType: audioBlob.type
                    });
                }
            };

            reader.onerror = () => reject(new Error('读取音频数据失败'));
            reader.readAsArrayBuffer(audioBlob.slice(0, 12)); // 只读取前12字节
        });
    }

    // 更新录音状态显示
    updateRecordingStatus(status, transcript = '') {
        const recordingDisplay = document.getElementById('recordingDisplay');
        const statusText = document.getElementById('statusText');
        const transcriptionContainer = document.getElementById('transcriptionContainer');
        const recordingWave = document.getElementById('recordingWave');
        const startAnswerBtn = document.getElementById('startAnswerBtn');

        // 顶部只显示剩余时间，不再切换录音状态（避免闪烁）

        if (!recordingDisplay || !statusText) return;

        // 移除所有状态类
        recordingDisplay.classList.remove('active');

        switch (status) {
            case 'idle':
                statusText.textContent = '等待开始面试';
                // 不隐藏转写容器，让转写内容区域始终显示（移动端布局要求）
                // if (transcriptionContainer) transcriptionContainer.style.display = 'none';
                recordingWave.style.display = 'none';
                break;
            case 'ready':
                statusText.textContent = '准备中...';
                recordingDisplay.classList.add('active');
                // 不隐藏转写容器，让转写内容区域始终显示（移动端布局要求）
                // if (transcriptionContainer) transcriptionContainer.style.display = 'none';
                recordingWave.style.display = 'none';
                break;
            case 'recording':
                // 关闭 ASR 展示时：中间状态行只显示「录音中」，不把整段转写塞到 statusText（移动端尤其明显）
                statusText.textContent = (this.showAsrText && transcript) ? transcript : '录音中';
                recordingDisplay.classList.add('active');
                // 不在这里控制 transcriptionContainer 的显示，由 updateTranscriptDisplay() 统一控制
                // if (transcriptionContainer) transcriptionContainer.style.display = transcript ? 'block' : 'none';
                recordingWave.style.display = 'flex';
                if (startAnswerBtn) startAnswerBtn.classList.add('recording');
                break;
            case 'paused':
                statusText.textContent = '已暂停';
                // 不隐藏转写容器，让转写内容区域始终显示（移动端布局要求）
                // if (transcriptionContainer) transcriptionContainer.style.display = 'none';
                recordingWave.style.display = 'none';
                break;
            case 'completed':
                statusText.textContent = '面试完成';
                // 不隐藏转写容器，让转写内容区域始终显示（移动端布局要求）
                // if (transcriptionContainer) transcriptionContainer.style.display = 'none';
                recordingWave.style.display = 'none';
                break;
            default:
                statusText.textContent = '准备中...';
        }
    }

    // 在音频播放时禁用输入控件
    disableInputControls() {
        const startAnswerBtn = document.getElementById('startAnswerBtn');

        if (startAnswerBtn) {
            startAnswerBtn.disabled = true;
        }

        console.log('开始回答按钮已禁用 - 正在播放题目音频');
    }

    // 在音频播放结束后重新启用输入控件
    enableInputControls() {
        const startAnswerBtn = document.getElementById('startAnswerBtn');

        if (startAnswerBtn) {
            startAnswerBtn.disabled = false;
        }

        console.log('开始回答按钮已启用 - 可以开始录音回答问题');
    }

    // 语音录制相关

    // 开始回答（参考旧代码：发送manual_next_question消息）
    async finishAnswer() {
        try {
            console.log('🎯 完成回答，发送手动切换下一题请求...');
            
            // 解锁追问状态（用户已回答追问或当前问题）
            if (this.isFollowUpActive) {
                console.log('🔓 用户点击完成回答，解除追问状态锁定');
                this.isFollowUpActive = false;
            }

            // 检查是否正在切换中
            if (this.isSwitching) {
                console.warn('正在切换中，忽略本次操作');
                return;
            }

            // 检查WebSocket连接和录音状态
            // 支持RecordRTC和AudioManager两种方式
            const isRecording = (this.wsASRClient && this.wsASRClient.started) || 
                               (this.recordRTC && this.recordRTC.getState() === 'recording');
            
            if (!isRecording) {
                this.showError('请先开始录音');
                return;
            }

            // 发送前标记切换中，防止重复点击导致后端收到两次请求误判面试完成
            this.isSwitching = true;
            const success = this.wsASRClient.sendControlMessage('manual_next_question', {
                invitation_id: this.currentSession?.invitation_id || this.currentInvitationId,
                question_id: this.currentQuestion?.question_id,
                trigger_source: 'ui_button',
                timestamp: Date.now()
            });

            if (success) {
                console.log('✅ 已发送手动切换下一题请求');
                // 只有专业题点击“下一题”时，才显示“正在评分中，请稍候...”
                const currentType = typeof this.getCurrentQuestionType === 'function'
                    ? this.getCurrentQuestionType()
                    : (this.allQuestions?.[this.currentQuestionIndex]?.type || 'unknown');
                if (currentType === 'professional') {
                    this.showSuccess('正在评分中，请稍候...');
                }
                console.log('⏳ 等待评分结果，可能触发追问');
            } else {
                this.isSwitching = false;
                this.showError('发送切换请求失败，请重试');
            }

        } catch (error) {
            console.error('完成回答失败:', error);
            this.showError('提交回答失败: ' + error.message);
        }
    }

    async startAnswer() {
        if (this.isRecording) {
            console.log('🎤 startAnswer 已处于录音中，忽略');
            return;
        }
        if (!this.currentQuestion) {
            this.showError('请先获取问题');
            return;
        }

        try {
            console.log('🎤 开始回答，检查麦克风权限...');

            // 在移动设备上，权限请求必须在用户手势中进行
            await this.ensureMicrophonePermission();

            // 显示录音提示和时长检查
            await this.showRecordingGuidance();

            this.updateButtonStates('recording');

            // 记录录音开始时间
            this.recordingStartTime = Date.now();
            this.recordingDuration = 0;
            this.audioQualityMetrics = {
                sampleRate: null,
                channelCount: null,
                bitDepth: null,
                entropy: null,
                vadActivity: 0,
                totalSamples: 0
            };

            // 调试信息面板已移除（不再显示右上角音频调试信息）

            // 使用RecordRTC方式（参考旧代码）
            if (this.useNewAudioSystem) {
                await this.startNewAudioSystem();
            } else {
                // 使用RecordRTC方式（参考旧代码）
                await this.startDualRecordingWithVolumeDetection();
            }

            // 开始录音时长监控
            this.startRecordingDurationMonitor();

        } catch (error) {
            console.error('开始回答失败:', error);
            this.showError('开始回答失败: ' + error.message);
            this.updateButtonStates('active');
        }
    }
    
    // 启动新的音频系统（Web Audio API + AudioWorklets + WebSocket）
    async startNewAudioSystem() {
        try {
            console.log('🚀 启动新的音频系统...');
            
            // 初始化AudioManager - 使用16kHz采样率（与ASR服务一致）
            if (!this.audioManager) {
                this.audioManager = new AudioManager({
                    targetSampleRate: 16000, // ASR要求16kHz
                    sttSampleRate: 16000, // STT 16kHz
                    chunkSize: 2048, // 块大小
                    silenceThreshold: 0.7, // 静默阈值（秒）
                    maxQueueSize: 50, // 最大队列大小
                    workletUrl: '/static/js/audio-processor.js',
                    onAudioChunk: (chunk) => {
                        this.handleAudioChunk(chunk);
                    },
                    onSilenceDetected: (duration) => {
                        console.log(`🔇 检测到静默: ${duration.toFixed(2)}秒`);
                    },
                    onError: (error) => {
                        console.error('❌ AudioManager错误:', error);
                        this.showError('音频处理错误: ' + error.message);
                    }
                });
                
                await this.audioManager.initialize();
                console.log('✅ AudioManager初始化成功');
            }
            
            const invitationId = this.currentSession?.invitation_id || this.currentInvitationId;
            const questionId = this.currentQuestion?.question_id;
            if (!questionId) {
                throw new Error('当前题目ID不可用，无法启动录音');
            }

            // --- 统一后的 WebSocket ASR 初始化逻辑 ---
            if (!this.wsASRClient) {
                this.wsASRClient = new WebSocketASRClient({
                    endpoint: '/ws/asr',
                    onTranscript: (result) => {
                        const currentQuestionNum = this.currentQuestionIndex + 1;
                        const logMsg1 = `📝 [题目${currentQuestionNum}] 收到转写结果: type=${result.type}, text="${result.text}"`;
                        console.log(logMsg1);
                        if (window.addDebugLog) window.addDebugLog(logMsg1, 'transcript');
                        
                        const logMsg2 = `📝 [题目${currentQuestionNum}] 转写结果详情: ${JSON.stringify(result)}`;
                        console.log(logMsg2);
                        if (window.addDebugLog) window.addDebugLog(logMsg2, 'info');
                        
                        // 强制确保 transcriptionContainer 显示
                        const transcriptionContainer = document.getElementById('transcriptionContainer');
                        if (!transcriptionContainer) {
                            const msg = `❌ [题目${currentQuestionNum}] transcriptionContainer 元素不存在！`;
                            console.error(msg);
                            if (window.addDebugLog) window.addDebugLog(msg, 'error');
                            return;
                        }
                        
                        // 强制显示容器
                        const containerWasHidden = transcriptionContainer.style.display === 'none';
                        if (containerWasHidden) {
                            transcriptionContainer.style.display = 'flex';
                            const msg = `⚠️ [题目${currentQuestionNum}] 容器被隐藏，强制显示`;
                            console.warn(msg);
                            if (window.addDebugLog) window.addDebugLog(msg, 'warning');
                        }
                        
                        if (result.type === 'final') {
                            this.intermediateText = '';
                            this.transcribedText = result.text || '';
                            this.accumulatedText = this.transcribedText;
                            this.transcriptBuffer = this.transcribedText;
                            const msg = `✅ [题目${currentQuestionNum}] 更新最终文本: "${this.transcribedText}", 容器显示状态: ${transcriptionContainer.style.display}`;
                            console.log(msg);
                            if (window.addDebugLog) window.addDebugLog(msg, 'transcript');
                            this.updateTranscriptDisplay();
                        } else if (result.type === 'intermediate') {
                            this.intermediateText = result.text || '';
                            const msg = `🔄 [题目${currentQuestionNum}] 更新临时文本: "${this.intermediateText}", 容器显示状态: ${transcriptionContainer.style.display}`;
                            console.log(msg);
                            if (window.addDebugLog) window.addDebugLog(msg, 'transcript');
                            this.updateTranscriptDisplay();
                        }
                    },
                    onError: (error) => {
                        console.error('❌ WebSocket ASR错误:', error);
                        this.showError('语音识别错误: ' + error.message);
                    },
                    onEvaluation: (result) => {
                        console.log('📊 收到评分结果:', result);
                        if (result.need_follow_up && result.follow_up_question) {
                            // 评分结果只负责锁定；追问题干统一由 follow_up_trigger 展示，避免同一追问出现两次
                            this.isFollowUpActive = true; 
                            this.isSwitching = false;
                            console.log('🔒 评分触发追问：已锁定状态，等待 follow_up_trigger 展示题干');
                        } else {
                            this.isFollowUpActive = false;
                        }
                    },
                    onFollowUpTrigger: (data) => {
                        console.log('❓ 收到追问指令:', data);
                        this.isFollowUpActive = true;
                        // 重置切换状态，允许用户回答追问
                        this.isSwitching = false;
                        console.log('🔒 追问触发：已锁定状态，已重置isSwitching');
                        if (data.parent_answer_id && this._lastDisplayedFollowUpParentId === data.parent_answer_id) {
                            console.debug('追问已展示过，跳过重复展示:', data.parent_answer_id);
                            return;
                        }
                        if (data.parent_answer_id) {
                            this._lastDisplayedFollowUpParentId = data.parent_answer_id;
                        }
                        const q = data.question_for_tts || data.question || (data.data && data.data.question);
                        this.addFollowUpMessage(q);
                    },
                    onFollowUpPending: (msg) => {
                        this.isSwitching = false;
                        const text = msg.message || '请先回答追问后再进入下一题';
                        if (typeof this.showSuccess === 'function') {
                            this.showSuccess(text);
                        } else {
                            console.warn(text);
                        }
                    },
                    onNextQuestion: (data) => {
                        console.log('➡️ 收到下一题通知，准备 100ms 延迟拦截检测...');
                        
                        // 【核心改动】统一使用延迟检测，确保评分锁先到位
                        setTimeout(() => {
                            console.log('🔎 拦截检测状态:', this.isFollowUpActive);
                            if (this.isFollowUpActive === true) {
                                console.warn('🛑 拦截成功：当前处于追问锁定，禁止清空屏幕。');
                                return;
                            }

                            console.log('🧹 正常流程：清空屏幕，进入下一题');
                            this.isSwitching = false;
                            
                            if (data.autoAdvanced) {
                                this.showSuccess('检测到您已回答完毕，正在切换到下一题...');
                            }
                            
                            this.clearChatMessages();
                            this.transcriptBuffer = '';
                            this.intermediateText = '';
                            this.transcribedText = '';
                            this.accumulatedText = '';
                            this.clearTranscriptDisplay();

                            if (data.nextQuestionId) {
                                this.getCurrentQuestionAndUpdate();
                            } else {
                                void this.handleInterviewCompleted(data || {}).catch((err) => {
                                    console.error('面试完成收尾失败:', err);
                                });
                            }
                        }, 100); 
                    },
                    onInterviewCompleted: (data) => {
                        this.isSwitching = false;
                        void this.handleInterviewCompleted(data).catch((err) => {
                            console.error('面试完成收尾失败:', err);
                        });
                    },
                    onSilenceCountdown: (data) => {
                        // 处理静音倒计时
                        console.log('⏱️ 静音倒计时:', data.countdown);
                        // 可以显示倒计时UI
                    },
                    onConnect: () => {
                        console.log('✅ WebSocket ASR连接成功');
                        try {
                            if (typeof this.getCurrentQuestionAndUpdate === 'function') {
                                void this.getCurrentQuestionAndUpdate();
                            }
                        } catch (e) {
                            console.warn('WebSocket 连接成功后同步题目状态失败:', e);
                        }
                    },
                    onDisconnect: () => {
                        console.log('🔌 WebSocket ASR连接断开');
                    },
                    onRecordingStopped: () => {
                        this._resolveRecordingStoppedIfWaiting();
                    }
                });
            }

            await this.wsASRClient.connect({
                invitationId,
                questionId,
                sessionId: this.currentSession?.session_id || 'unknown'
            });
            console.log('✅ WebSocket ASR客户端准备就绪');
            
            // 开始录音
            await this.audioManager.startRecording();
            if (this.audioManager.mediaStream) {
                this.initAudioAnalysis(this.audioManager.mediaStream);
            }
            console.log('✅ 新音频系统启动成功');
            
            // 更新UI状态
            this.isRealTimeMode = true;
            this.recordingStartTime = Date.now();
            this.transcriptBuffer = '';
            this.intermediateText = '';
            this.transcribedText = '';
            this.accumulatedText = '';
            
            // 强制显示整个 transcription-section（第一题可能 updateCurrentQuestion 没有被调用）
            // 使用"暴力"样式注入
            const transcriptionSection = document.querySelector('.transcription-section');
            if (!transcriptionSection) {
                console.error('❌ [深度排查] startNewAudioSystem: 错误：在 DOM 中根本找不到 .transcription-section 元素！');
                if (window.addDebugLog) window.addDebugLog('❌ [深度排查] startNewAudioSystem: transcription-section 元素不存在', 'error');
            } else {
                console.log('✅ [深度排查] startNewAudioSystem: 找到 transcription-section，确保显示');
                // 只设置必要的显示属性，不覆盖flex布局
                const currentDisplay = window.getComputedStyle(transcriptionSection).display;
                if (currentDisplay === 'none') {
                    transcriptionSection.style.display = 'flex';
                }
                transcriptionSection.style.visibility = 'visible';
                transcriptionSection.style.opacity = '1';
                // 不设置flex值，让CSS自然布局
                if (window.addDebugLog) window.addDebugLog('✅ startNewAudioSystem: 确保显示 transcription-section', 'container');
            }
            
            const transcriptionContent = document.getElementById('transcriptionContent');
            if (transcriptionContent) {
                transcriptionContent.setAttribute('style', 'display: block !important; visibility: visible !important; opacity: 1 !important;');
            }
            
            // 显示转写区域并提示"等待语音输入"，便于边说话边显示文字
            const tc = document.getElementById('transcriptionContainer');
            if (tc) { 
                tc.setAttribute('style', 'display: flex !important; visibility: visible !important; opacity: 1 !important;');
                console.log('✅ 第一题（新音频系统）：强制显示转写容器');
                if (window.addDebugLog) window.addDebugLog('✅ startNewAudioSystem: 强制显示 transcription-container', 'container');
            }
            
            // 清空并初始化转写显示（不清空容器显示状态）
            const transcriptText = document.getElementById('transcriptText');
            const intermediateTextEl = document.getElementById('intermediateText');
            const emptyTextEl = document.getElementById('emptyText');
            if (transcriptText) transcriptText.textContent = '';
            if (intermediateTextEl) intermediateTextEl.textContent = '';
            if (emptyTextEl) {
                emptyTextEl.style.display = 'block';
                emptyTextEl.textContent = '等待语音输入...';
            }
            
            this.updateTranscriptDisplay();
            this.syncTranscriptionVisualModeAfterRecordStart();
            
        } catch (error) {
            console.error('❌ 启动新音频系统失败:', error);
            throw error;
        }
    }
    
    // 处理音频块（新系统）
    handleAudioChunk(chunk) {
        if (this.wsASRClient && this.wsASRClient.isConnected) {
            // 发送到WebSocket服务器
            this.wsASRClient.sendAudioChunk(chunk);
        } else {
            console.warn('⚠️ WebSocket未连接，音频块未发送');
        }
    }

    // 显示录音指导和用户提示（已移除提示信息）
    async showRecordingGuidance() {
        // 不再显示任何提示信息，直接返回
        return Promise.resolve();
    }

    // 调试信息面板已移除（用户要求去掉右上角音频调试信息）
    showDebugInfo() {}
    updateDebugInfo() {}
    hideDebugInfo() {}

    // 开始录音时长监控（仅内部计时，不再更新调试面板和顶部状态，避免闪烁）
    startRecordingDurationMonitor() {
        this.recordingDuration = 0;
        this.recordingDurationInterval = setInterval(() => {
            this.recordingDuration = (Date.now() - this.recordingStartTime) / 1000;
        }, 100);
    }

    // 显示录音状态
    showRecordingStatus(type, message) {
        const statusDiv = document.getElementById('recordingStatus');
        if (!statusDiv) {
            const newStatusDiv = document.createElement('div');
            newStatusDiv.id = 'recordingStatus';
            newStatusDiv.style.cssText = `
                position: fixed;
                bottom: 100px;
                left: 50%;
                transform: translateX(-50%);
                padding: 8px 16px;
                border-radius: 20px;
                font-size: 14px;
                font-weight: bold;
                z-index: 1000;
                transition: all 0.3s ease;
            `;
            document.body.appendChild(newStatusDiv);
        }

        const statusDiv2 = document.getElementById('recordingStatus');
        statusDiv2.textContent = message;
        statusDiv2.style.display = 'block';

        if (type === 'good') {
            statusDiv2.style.background = '#4caf50';
            statusDiv2.style.color = 'white';
        } else if (type === 'warning') {
            statusDiv2.style.background = '#ff9800';
            statusDiv2.style.color = 'white';
        } else {
            statusDiv2.style.background = '#2196f3';
            statusDiv2.style.color = 'white';
        }

        // 2秒后自动隐藏
        setTimeout(() => {
            if (statusDiv2) {
                statusDiv2.style.display = 'none';
            }
        }, 2000);
    }

    // 确保麦克风权限（专门为移动设备优化）
    async ensureMicrophonePermission() {
        console.log('🔐 确保麦克风权限...');

        // 检查是否支持权限API
        if (navigator.permissions) {
            try {
                const permissionStatus = await navigator.permissions.query({ name: 'microphone' });
                console.log('麦克风权限状态:', permissionStatus.state);

                if (permissionStatus.state === 'denied') {
                    throw new Error('麦克风权限已被拒绝，请在浏览器设置中允许访问麦克风');
                }

                if (permissionStatus.state === 'granted') {
                    console.log('✅ 麦克风权限已授予');
                    return;
                }

                // 如果状态是 'prompt' 或 'unknown'，尝试请求权限
                console.log('📋 权限状态需要确认，请求麦克风权限...');
            } catch (error) {
                console.warn('权限查询失败:', error);
                // 继续尝试直接请求权限
            }
        } else {
            console.log('⚠️ 浏览器不支持权限API，直接请求权限');
        }

        // 直接请求麦克风权限（这会触发浏览器权限弹窗）
        try {
            console.log('🎙️ 请求麦克风权限...');

            // 检查浏览器支持
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                throw new Error('您的浏览器不支持麦克风访问功能。建议使用Chrome、Firefox、Safari等现代浏览器，并确保页面通过HTTPS访问。');
            }

            // 检测是否为移动设备
            const isMobile = /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);

            // 移动设备优化：使用更兼容的音频约束
            const audioConstraints = {
                audio: {
                    echoCancellation: true,
                    noiseSuppression: true,
                    autoGainControl: true,
                    sampleRate: 44100,
                    channelCount: 1,
                    // 移动设备可能不支持高级约束，使用基本设置
                    ...(isMobile ? {} : {
                        latency: 0.01,
                        volume: 1.0
                    })
                }
            };

            const stream = await navigator.mediaDevices.getUserMedia(audioConstraints);

            // 立即停止流，我们只是为了获取权限
            stream.getTracks().forEach(track => track.stop());
            console.log('✅ 麦克风权限获取成功');

        } catch (error) {
            console.error('❌ 麦克风权限获取失败:', error);

            let errorMessage = '无法访问麦克风';
            if (error.name === 'NotAllowedError') {
                errorMessage = '麦克风权限被拒绝。请点击浏览器的地址栏，允许麦克风访问。';
            } else if (error.name === 'NotFoundError') {
                errorMessage = '未找到麦克风设备';
            } else if (error.name === 'NotReadableError') {
                errorMessage = '麦克风被其他应用占用';
            } else if (error.name === 'OverconstrainedError') {
                errorMessage = '麦克风配置不支持';
            } else if (error.name === 'SecurityError') {
                errorMessage = '安全策略阻止麦克风访问（请确保使用HTTPS或localhost）';
            }

            throw new Error(errorMessage);
        }
    }

    // 开始双缓存录音并进行音量检测
    // 使用RecordRTC进行录音（参考旧代码）
    async startDualRecordingWithVolumeDetection() {
        try {
            console.log('🎤 startDualRecordingWithVolumeDetection 进入（RecordRTC 方式）');
            if (window.addDebugLog) window.addDebugLog('🎤 startDualRecordingWithVolumeDetection 开始执行', 'info');
            
            // 检查RecordRTC是否可用
            if (typeof RecordRTC === 'undefined') {
                console.error('❌ RecordRTC库未加载！');
                console.error('请检查CDN链接是否正常：https://cdn.jsdelivr.net/npm/recordrtc@5.6.2/RecordRTC.min.js');
                throw new Error('RecordRTC库未加载，请刷新页面重试');
            }
            console.log('✅ RecordRTC 已加载');

            // 标记为实时模式
            this.isRealTimeMode = true;

            // 获取麦克风权限和音频流（参考旧代码）
            const getMedia = navigator.mediaDevices?.getUserMedia?.bind(navigator.mediaDevices);
            if (!getMedia) {
                throw new Error('浏览器不支持getUserMedia API');
            }

            const stream = await getMedia({
                audio: {
                    sampleRate: 16000,
                    channelCount: 1,
                    // 关键：关闭浏览器端的实时DSP，降低CPU抖动和时序不稳定
                    echoCancellation: false,
                    noiseSuppression: false,
                    autoGainControl: false,
                    latency: 0
                }
            });

            // 保存音频流引用
            this.stream = stream;

            this.initAudioAnalysis(stream);

            // 初始化WebSocket ASR客户端（如果还没有初始化）
            const invitationId = this.currentSession?.invitation_id || this.currentInvitationId;
            const questionId = this.currentQuestion?.question_id;
            if (!questionId) {
                throw new Error('当前题目ID不可用，无法启动录音');
            }

            // --- 统一后的 WebSocket ASR 初始化逻辑 ---
            if (!this.wsASRClient) {
                this.wsASRClient = new WebSocketASRClient({
                    endpoint: '/ws/asr',
                    onTranscript: (result) => {
                        const currentQuestionNum = this.currentQuestionIndex + 1;
                        const logMsg1 = `📝 [题目${currentQuestionNum}] 收到转写结果: type=${result.type}, text="${result.text}"`;
                        console.log(logMsg1);
                        if (window.addDebugLog) window.addDebugLog(logMsg1, 'transcript');
                        
                        const logMsg2 = `📝 [题目${currentQuestionNum}] 转写结果详情: ${JSON.stringify(result)}`;
                        console.log(logMsg2);
                        if (window.addDebugLog) window.addDebugLog(logMsg2, 'info');
                        
                        // 强制确保 transcriptionContainer 显示
                        const transcriptionContainer = document.getElementById('transcriptionContainer');
                        if (!transcriptionContainer) {
                            const msg = `❌ [题目${currentQuestionNum}] transcriptionContainer 元素不存在！`;
                            console.error(msg);
                            if (window.addDebugLog) window.addDebugLog(msg, 'error');
                            return;
                        }
                        
                        // 强制显示容器
                        const containerWasHidden = transcriptionContainer.style.display === 'none';
                        if (containerWasHidden) {
                            transcriptionContainer.style.display = 'flex';
                            const msg = `⚠️ [题目${currentQuestionNum}] 容器被隐藏，强制显示`;
                            console.warn(msg);
                            if (window.addDebugLog) window.addDebugLog(msg, 'warning');
                        }
                        
                        if (result.type === 'final') {
                            this.intermediateText = '';
                            this.transcribedText = result.text || '';
                            this.accumulatedText = this.transcribedText;
                            this.transcriptBuffer = this.transcribedText;
                            const msg = `✅ [题目${currentQuestionNum}] 更新最终文本: "${this.transcribedText}", 容器显示状态: ${transcriptionContainer.style.display}`;
                            console.log(msg);
                            if (window.addDebugLog) window.addDebugLog(msg, 'transcript');
                            this.updateTranscriptDisplay();
                        } else if (result.type === 'intermediate') {
                            this.intermediateText = result.text || '';
                            const msg = `🔄 [题目${currentQuestionNum}] 更新临时文本: "${this.intermediateText}", 容器显示状态: ${transcriptionContainer.style.display}`;
                            console.log(msg);
                            if (window.addDebugLog) window.addDebugLog(msg, 'transcript');
                            this.updateTranscriptDisplay();
                        }
                    },
                    onError: (error) => {
                        console.error('❌ WebSocket ASR错误:', error);
                        this.showError('语音识别错误: ' + error.message);
                    },
                    onEvaluation: (result) => {
                        console.log('📊 收到评分结果:', result);
                        if (result.need_follow_up && result.follow_up_question) {
                            // 评分结果只负责锁定；追问题干统一由 follow_up_trigger 展示，避免同一追问出现两次
                            this.isFollowUpActive = true; 
                            this.isSwitching = false;
                            console.log('🔒 评分触发追问：已锁定状态，等待 follow_up_trigger 展示题干');
                        } else {
                            this.isFollowUpActive = false;
                        }
                    },
                    onFollowUpTrigger: (data) => {
                        console.log('❓ 收到追问指令:', data);
                        this.isFollowUpActive = true;
                        // 重置切换状态，允许用户回答追问
                        this.isSwitching = false;
                        console.log('🔒 追问触发：已锁定状态，已重置isSwitching');
                        if (data.parent_answer_id && this._lastDisplayedFollowUpParentId === data.parent_answer_id) {
                            console.debug('追问已展示过，跳过重复展示:', data.parent_answer_id);
                            return;
                        }
                        if (data.parent_answer_id) {
                            this._lastDisplayedFollowUpParentId = data.parent_answer_id;
                        }
                        const q = data.question_for_tts || data.question || (data.data && data.data.question);
                        this.addFollowUpMessage(q);
                    },
                    onFollowUpPending: (msg) => {
                        this.isSwitching = false;
                        const text = msg.message || '请先回答追问后再进入下一题';
                        if (typeof this.showSuccess === 'function') {
                            this.showSuccess(text);
                        } else {
                            console.warn(text);
                        }
                    },
                    onNextQuestion: (data) => {
                        console.log('➡️ 收到下一题通知，准备 100ms 延迟拦截检测...');
                        
                        // 【核心改动】统一使用延迟检测，确保评分锁先到位
                        setTimeout(() => {
                            console.log('🔎 拦截检测状态:', this.isFollowUpActive);
                            if (this.isFollowUpActive === true) {
                                console.warn('🛑 拦截成功：当前处于追问锁定，禁止清空屏幕。');
                                return;
                            }

                            console.log('🧹 正常流程：清空屏幕，进入下一题');
                            this.isSwitching = false;
                            
                            if (data.autoAdvanced) {
                                this.showSuccess('检测到您已回答完毕，正在切换到下一题...');
                            }
                            
                            this.clearChatMessages();
                            this.transcriptBuffer = '';
                            this.intermediateText = '';
                            this.transcribedText = '';
                            this.accumulatedText = '';
                            this.clearTranscriptDisplay();

                            if (data.nextQuestionId) {
                                this.getCurrentQuestionAndUpdate();
                            } else {
                                void this.handleInterviewCompleted(data || {}).catch((err) => {
                                    console.error('面试完成收尾失败:', err);
                                });
                            }
                        }, 100); 
                    },
                    onInterviewCompleted: (data) => {
                        this.isSwitching = false;
                        void this.handleInterviewCompleted(data).catch((err) => {
                            console.error('面试完成收尾失败:', err);
                        });
                    },
                    onConnect: () => {
                        console.log('✅ WebSocket ASR连接成功');
                        try {
                            if (typeof this.getCurrentQuestionAndUpdate === 'function') {
                                void this.getCurrentQuestionAndUpdate();
                            }
                        } catch (e) {
                            console.warn('WebSocket 连接成功后同步题目状态失败:', e);
                        }
                    },
                    onDisconnect: () => {
                        console.log('🔌 WebSocket ASR连接断开');
                    },
                    onRecordingStopped: () => {
                        this._resolveRecordingStoppedIfWaiting();
                    }
                });
            }

            // 连接WebSocket（参考旧代码：先连接，再创建RecordRTC）
            await this.wsASRClient.connect({
                invitationId,
                questionId,
                sessionId: this.currentSession?.session_id || 'unknown'
            });

            // 参考旧代码：直接创建RecordRTC，不需要等待recording_started
            // 创建RecordRTC实例（参考旧代码）
            this.recordRTC = new RecordRTC(stream, {
                type: 'audio',
                mimeType: 'audio/wav',
                recorderType: RecordRTC.StereoAudioRecorder,
                numberOfAudioChannels: 1,
                desiredSampRate: 16000,
                // 缩短 timeSlice，使音频块更细、更平滑（降低主线程压力）
                timeSlice: 200, // 每200ms发送一次音频片段
                ondataavailable: (blob) => {
                    // 若有 WAV 头则去头后仅发裸 PCM，否则整体发送（兼容仅首块带头的实现）
                    if (blob && blob.size > 0) {
                        blob.arrayBuffer().then((buffer) => {
                            if (buffer.byteLength === 0) return;
                            const WAV_HEADER_SIZE = 44;
                            const isWav = buffer.byteLength >= 4 && new Uint8Array(buffer)[0] === 0x52 && new Uint8Array(buffer)[1] === 0x49 && new Uint8Array(buffer)[2] === 0x46 && new Uint8Array(buffer)[3] === 0x46; // "RIFF"
                            const toSend = (isWav && buffer.byteLength > WAV_HEADER_SIZE) ? buffer.slice(WAV_HEADER_SIZE) : buffer;
                            this.recordRTCAudioQueue.push(toSend);
                        }).catch((error) => {
                            console.error('[音频] 解析 blob 失败:', error);
                        });
                    }

                    // 启动定时发送任务（如果尚未启动）
                    if (!this.recordRTCSendTimer) {
                        this.recordRTCSendTimer = setInterval(() => {
                            if (!this.wsASRClient ||
                                !this.wsASRClient.ws ||
                                this.wsASRClient.ws.readyState !== WebSocket.OPEN ||
                                !this.isRecording) {
                                return;
                            }
                            const nextChunk = this.recordRTCAudioQueue.shift();
                            if (nextChunk) {
                                this.wsASRClient.ws.send(nextChunk);
                                if (Math.random() < 0.1) {
                                    console.log(`[音频] 发送片段: ${nextChunk.byteLength} 字节, 队列剩余: ${this.recordRTCAudioQueue.length}`);
                                }
                            }
                        }, 100);
                    }
                }
            });

            // 开始录制命令由 WebSocketASRClient 在 connection_established 时自动发送（_startRecordingSession），此处不再重复发送

            // 开始录音（参考旧代码：RecordRTC.startRecording()）
            this.recordRTC.startRecording();
            console.log('[录音] RecordRTC录音已开始');

            // 初始化录音状态
            this.isRecording = true;
            this.recordingStartTime = Date.now();
            this.transcriptBuffer = '';
            this.intermediateText = '';
            this.transcribedText = '';
            this.accumulatedText = '';
            
            // 确保转写区域显示（第一题可能 updateCurrentQuestion 没有被调用）
            const transcriptionSection = document.querySelector('.transcription-section');
            if (transcriptionSection) {
                // 只确保display不为none，让CSS flex布局自然工作
                const currentDisplay = window.getComputedStyle(transcriptionSection).display;
                if (currentDisplay === 'none') {
                    transcriptionSection.style.display = 'flex';
                }
                
                // 检查元素是否在可视区域内，如果不在则滚动
                const rect = transcriptionSection.getBoundingClientRect();
                const viewportHeight = window.innerHeight;
                if (rect.top >= viewportHeight || rect.bottom <= 0) {
                    console.warn('⚠️ transcription-section 不在可视区域内，尝试滚动');
                    transcriptionSection.scrollIntoView({ behavior: 'smooth', block: 'end' });
                }
            }
            
            const transcriptionContent = document.getElementById('transcriptionContent');
            if (transcriptionContent && transcriptionContent.style.display === 'none') {
                transcriptionContent.style.display = 'block';
            }
            
            const tc = document.getElementById('transcriptionContainer');
            if (tc && tc.style.display === 'none') {
                tc.style.display = 'flex';
            }
            
            // 清空并初始化转写显示（不清空容器显示状态）
            const transcriptText = document.getElementById('transcriptText');
            const intermediateTextEl = document.getElementById('intermediateText');
            const emptyTextEl = document.getElementById('emptyText');
            if (transcriptText) transcriptText.textContent = '';
            if (intermediateTextEl) intermediateTextEl.textContent = '';
            if (emptyTextEl) {
                emptyTextEl.style.display = 'block';
                emptyTextEl.textContent = '等待语音输入...';
            }
            
            this.updateTranscriptDisplay();
            this.syncTranscriptionVisualModeAfterRecordStart();

            console.log('✅ RecordRTC录音已开始');
            if (window.addDebugLog) window.addDebugLog('✅ RecordRTC录音已开始', 'success');

        } catch (error) {
            console.error('❌ [深度排查] 开始RecordRTC录音失败:', error);
            console.error('❌ [深度排查] 错误堆栈:', error.stack);
            if (window.addDebugLog) {
                window.addDebugLog(`❌ [深度排查] startDualRecording 失败: ${error.message}`, 'error');
                window.addDebugLog(`❌ [深度排查] 错误堆栈: ${error.stack}`, 'error');
            }
            throw error;
        }
    }

    // 启动实时转录（激进优化版本）
    startRealTimeTranscription() {
        console.log('🎤 启动实时转录（激进优化版本）');

        let processedCount = 0;
        let lastForceSendTime = Date.now();

        // 更激进的策略：更频繁检查，更早发送
        this.transcriptionInterval = setInterval(async () => {
            try {
                const currentChunkCount = this.currentQuestionChunks.length;
                const now = Date.now();

                // 如果有未处理的音频块
                if (currentChunkCount > processedCount) {
                    console.log(`📦 发现 ${currentChunkCount - processedCount} 个新音频块`);

                    // 合并所有可用的块
                    const chunksToProcess = this.currentQuestionChunks.slice(processedCount);
                    const totalSize = chunksToProcess.reduce((sum, chunk) => sum + chunk.size, 0);

                    console.log(`🔄 合并 ${chunksToProcess.length} 个音频块，总大小: ${totalSize} bytes`);

                    // 大幅降低发送阈值
                    if (totalSize > 500) { // 从1000降到500字节
                        await this.sendAccumulatedAudio(chunksToProcess, totalSize);
                        processedCount = currentChunkCount;
                        lastForceSendTime = now;
                        console.log(`✅ 已处理 ${processedCount} 个音频块`);
                    } else {
                        console.log(`⏳ 音频数据太小 (${totalSize} bytes)，等待更多数据`);
                    }
                }

                // 更频繁的强制发送（每3秒）
                if (now - lastForceSendTime > 3000 && processedCount < this.currentQuestionChunks.length) {
                    console.log('⏰ 强制发送所有积累的音频数据（3秒超时）');
                    const chunksToProcess = this.currentQuestionChunks.slice(processedCount);
                    const totalSize = chunksToProcess.reduce((sum, chunk) => sum + chunk.size, 0);

                    if (chunksToProcess.length > 0) {
                        await this.sendAccumulatedAudio(chunksToProcess, totalSize);
                        processedCount = this.currentQuestionChunks.length;
                        lastForceSendTime = now;
                    }
                }

            } catch (error) {
                console.error('❌ 实时转录处理错误:', error);
            }
        }, 300); // 从500ms降到300ms，更频繁检查

        console.log('🎤 实时转录已启动（激进优化版本）');
    }

    // 发送积累的音频数据
    async sendAccumulatedAudio(chunks, totalSize) {
        try {
            const mergedBlob = new Blob(chunks, { type: chunks[0].type });
            console.log(`🎵 发送合并音频块: ${chunks.length}个块，${totalSize} bytes (约${(totalSize / 16000 / 2).toFixed(2)}秒)`);

            await this.processAudioChunk(mergedBlob);
        } catch (mergeError) {
            console.error('音频块合并失败:', mergeError);
            // 如果合并失败，尝试处理单个最大的块
            if (chunks.length > 0) {
                const largestChunk = chunks.reduce((max, chunk) =>
                    chunk.size > max.size ? chunk : max
                );
                if (largestChunk.size >= 1000) { // 进一步降低最小块要求
                    console.log(`发送最大单个块: ${largestChunk.size} bytes`);
                    await this.processAudioChunk(largestChunk);
                } else {
                    console.warn(`最大块也太小: ${largestChunk.size} bytes，跳过发送`);
                }
            }
        }
    }

    // 处理完整音频Blob（核心方法）
    async processCompleteAudio(audioBlob) {
        console.log(`🎵 开始处理完整音频: ${audioBlob.size} bytes`);

        // 显示处理状态
        this.showLoading('语音识别', '正在识别语音内容...');

        try {
            // 转换为Base64
            const reader = new FileReader();
            const base64Promise = new Promise((resolve, reject) => {
                reader.onloadend = () => {
                    try {
                        const base64Audio = reader.result.split(',')[1];
                        resolve(base64Audio);
                    } catch (e) {
                        reject(e);
                    }
                };
                reader.onerror = reject;
            });

            reader.readAsDataURL(audioBlob);
            const base64Audio = await base64Promise;

            console.log(`📤 发送音频数据: ${base64Audio.length} 字符`);

            // 发送到ASR服务
            const result = await window.API.interview.submitVoiceAnswer(
                this.currentSession.session_id,
                this.currentQuestion.id,
                base64Audio,
                this.currentMimeType
            );

            console.log('🎉 ASR识别结果:', result);

            if (result && result.transcript && result.transcript.trim()) {
                console.log(`✅ 识别成功: "${result.transcript}"`);

                // 显示识别结果
                this.addBotMessage(`语音识别结果: "${result.transcript}"`);

                // 清空音频块缓存
                this.audioChunks = [];

                this.hideLoading();

                // 自动提交文本答案
                await this.submitTextAnswerFromTranscript(result.transcript);

            } else {
                console.warn('⚠️ 识别结果为空');
                this.hideLoading();
                this.showError('语音识别失败，请重试');
            }

        } catch (error) {
            console.error('❌ 处理完整音频失败:', error);
            this.hideLoading();
            this.showError('语音处理失败: ' + error.message);
        }
    }

    // 从转录结果提交文本答案
    async submitTextAnswerFromTranscript(transcript) {
        try {
            console.log(`📝 提交文本答案: "${transcript}"`);

            const result = await window.API.interview.submitTextAnswer(
                this.currentSession.session_id,
                this.currentQuestion.id,
                transcript
            );

            if (result && result.success) {
                console.log('✅ 文本答案提交成功');
                this.addBotMessage('答案已提交成功！');

                // 进入下一题
                setTimeout(() => {
                    this.nextQuestion();
                }, 2000);

            } else {
                console.warn('⚠️ 文本答案提交失败:', result);
                this.showError('答案提交失败，请重试');
            }

        } catch (error) {
            console.error('❌ 提交文本答案失败:', error);
            this.showError('答案提交失败: ' + error.message);
        }
    }

    // 手动触发音频发送（调试用）
    forceSendAudio() {
        if (this.audioChunks && this.audioChunks.length > 0) {
            console.log('🔧 手动触发音频发送');
            const totalSize = this.audioChunks.reduce((sum, chunk) => sum + chunk.size, 0);
            const completeAudioBlob = new Blob(this.audioChunks, { type: this.currentMimeType });
            this.processCompleteAudio(completeAudioBlob);
        } else {
            console.warn('⚠️ 没有音频数据可发送');
        }
    }

    // 处理音频块并进行实时转录
    async processAudioChunk(audioBlob) {
        try {
            // 检查音频数据大小，避免发送过小的块
            const MIN_AUDIO_SIZE = 3000; // 提高到3KB，约0.2秒的音频数据
            if (audioBlob.size < MIN_AUDIO_SIZE) {
                console.log(`音频块过小 (${audioBlob.size} bytes)，需要至少 ${MIN_AUDIO_SIZE} bytes，跳过处理`);
                return;
            }

            // 校验音频数据完整性，并获取真实格式
            let detectedFormat = 'unknown';
            let validationPassed = false;

            try {
                const validation = await this.validateAudioData(audioBlob);
                detectedFormat = validation.format;
                validationPassed = true;
                console.log(`音频校验通过: 格式=${detectedFormat}, 大小=${validation.size} bytes`);
            } catch (validationError) {
                console.error('音频数据校验失败:', validationError.message);
                // 对于校验失败的音频，仍然尝试发送，但记录警告
                console.warn('继续处理校验失败的音频数据...');
                // 回退到MIME类型推断
                if (audioBlob.type.includes('wav')) {
                    detectedFormat = 'WAV';
                } else if (audioBlob.type.includes('webm')) {
                    detectedFormat = 'WebM';
                } else if (audioBlob.type.includes('ogg')) {
                    detectedFormat = 'OGG';
                } else {
                    detectedFormat = 'unknown';
                }
            }

            console.log(`处理音频块: ${audioBlob.size} bytes, 检测格式: ${detectedFormat} (使用FormData传输)`);

            // 使用FormData进行二进制传输（避免Base64 33%损耗）
            const formData = new FormData();
            // 根据检测到的真实格式确定文件名扩展名
            let fileExtension = 'audio';
            if (detectedFormat === 'WAV') {
                fileExtension = 'wav';
            } else if (detectedFormat === 'WebM') {
                fileExtension = 'webm';
            } else if (detectedFormat === 'OGG') {
                fileExtension = 'ogg';
            } else if (detectedFormat === 'MP3') {
                fileExtension = 'mp3';
            }

            const fileName = `audio.${fileExtension}`;
            formData.append('audio', audioBlob, fileName); // 使用检测到的真实格式
            formData.append('session_id', this.currentSession?.session_id || '');
            formData.append('format', detectedFormat.toLowerCase()); // 传递检测到的真实格式

            try {
                // 调用实时ASR服务（FormData传输，无需Base64编码）
                const response = await fetch('/api/v1/asr/realtime', {
                    method: 'POST',
                    body: formData  // 直接发送二进制数据
                });

                    if (response.ok) {
                        const result = await response.json();
                        if (result.code === 200 && result.data && result.data.transcript) {
                            this.updateTranscript(result.data.transcript);
                            console.log('🎤 语音识别结果:', result.data.transcript);
                        } else if (result.message) {
                            console.log(`ASR响应: ${result.message}`);
                        }
                    } else {
                        console.error(`ASR请求失败: ${response.status}`);
                    }
            } catch (fetchError) {
                console.error('FormData传输失败:', fetchError);
                // 回退到Base64传输
                console.warn('尝试回退到Base64传输...');
                await this._fallbackBase64Transmission(audioBlob);
            }
        } catch (error) {
            console.error('处理音频块异常:', error);
        }
    }

    // Base64回退传输方法（仅在FormData失败时使用）
    async _fallbackBase64Transmission(audioBlob) {
        try {
            const reader = new FileReader();
            reader.onloadend = async () => {
                const base64Audio = reader.result.split(',')[1];

                const response = await fetch('/api/v1/asr/realtime', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({
                        audio_data: base64Audio,
                        session_id: this.currentSession?.session_id,
                        format: this.currentMimeType || 'audio/webm'
                    })
                });

                if (response.ok) {
                    const result = await response.json();
                    if (result.code === 200 && result.data && result.data.transcript) {
                        this.updateTranscript(result.data.transcript);
                        console.log('Base64回退转录成功:', result.data.transcript);
                    }
                }
            };

            reader.onerror = () => console.error('Base64读取失败');
            reader.readAsDataURL(audioBlob);
        } catch (error) {
            console.error('Base64回退传输失败:', error);
        }
    }

    // 高级音频格式检测（处理变体和特殊情况）
    _detectAudioFormatAdvanced(uint8Array, headerHex) {
        // 检查WebM变体（头部可能不完整）
        for (let i = 0; i < Math.min(16, uint8Array.length - 3); i++) {
            if (uint8Array[i] === 0x1A &&
                uint8Array[i+1] === 0x45 &&
                uint8Array[i+2] === 0xDF &&
                uint8Array[i+3] === 0xA3) {
                return 'WebM';
            }
        }

        // 检查MP3帧同步（更宽松的检测）
        for (let i = 0; i < Math.min(32, uint8Array.length - 1); i++) {
            const byte1 = uint8Array[i];
            const byte2 = uint8Array[i+1];
            if ((byte1 & 0xFF) === 0xFF && ((byte2 & 0xE0) === 0xE0)) {
                return 'MP3';
            }
        }

        // 检查AAC ADTS头部
        for (let i = 0; i < Math.min(32, uint8Array.length - 1); i++) {
            const byte1 = uint8Array[i];
            const byte2 = uint8Array[i+1];
            if ((byte1 & 0xFF) === 0xFF && ((byte2 & 0xF0) === 0xF0)) {
                return 'AAC';
            }
        }

        return null;
    }

    // 根据浏览器行为推断音频格式
    _inferBrowserAudioFormat(uint8Array, mimeType) {
        // 如果Blob有明确的MIME类型，优先使用
        if (mimeType) {
            const mimeToFormat = {
                'audio/wav': 'WAV',
                'audio/wave': 'WAV',
                'audio/webm': 'WebM',
                'audio/webm;codecs=opus': 'WebM',
                'audio/webm;codecs=vorbis': 'WebM',
                'audio/mp4': 'MP4',
                'audio/mpeg': 'MP3',
                'audio/ogg': 'OGG',
                'audio/ogg;codecs=opus': 'OGG'
            };

            if (mimeToFormat[mimeType]) {
                return mimeToFormat[mimeType];
            }
        }

        // 如果没有MIME类型但数据看起来有结构，可能是某种容器格式
        if (uint8Array.length > 100) {
            // 检查是否有明显的容器特征
            const firstBytes = Array.from(uint8Array.slice(0, 4));

            // 检查是否可能是某种二进制音频数据
            if (firstBytes[0] > 0 && firstBytes[0] < 128) {
                // 可能是某种音频数据的变体
                console.log('检测到可能的音频数据变体');
                return 'WebM'; // 默认假设是WebM，因为前端通常使用WebM
            }
        }

        return null;
    }

    // 更新转录文本显示（参考旧代码：同时显示最终文本和临时文本）
    updateTranscriptDisplay() {
        const transcriptionContainer = document.getElementById('transcriptionContainer');
        const transcriptText = document.getElementById('transcriptText');
        const intermediateTextEl = document.getElementById('intermediateText');
        const emptyTextEl = document.getElementById('emptyText');

        if (!transcriptionContainer) {
            const msg = '⚠️ 转写容器不存在';
            console.warn(msg);
            if (window.addDebugLog) window.addDebugLog(msg, 'error');
            return;
        }

        // 记录容器状态
        const containerWasHidden = transcriptionContainer.style.display === 'none';
        const currentQuestionNum = this.currentQuestionIndex + 1;
        
        // 强制显示转写容器（使用flex以符合CSS要求）
        transcriptionContainer.style.display = 'flex';
        const logMsg = `📦 [题目${currentQuestionNum}] 转写容器显示状态: ${containerWasHidden ? '从隐藏变为显示' : '已显示'}, transcribedText: "${this.transcribedText}", intermediateText: "${this.intermediateText}"`;
        console.log(logMsg);
        if (window.addDebugLog) window.addDebugLog(logMsg, 'container');

        // 控制自动跳转提示的显示
        const autoAdvanceTip = document.getElementById('autoAdvanceTip');
        
        // 如果有最终文本或临时文本，显示内容
        if (this.transcribedText || this.intermediateText) {
            if (transcriptText) {
                transcriptText.textContent = this.transcribedText || '';
                transcriptText.style.display = this.transcribedText ? 'block' : 'none';
                const msg = `📄 [题目${currentQuestionNum}] 最终文本元素已更新: "${transcriptText.textContent}"`;
                console.log(msg);
                if (window.addDebugLog) window.addDebugLog(msg, 'transcript');
            }
            if (intermediateTextEl) {
                intermediateTextEl.textContent = this.intermediateText || '';
                intermediateTextEl.style.display = this.intermediateText ? 'block' : 'none';
                const msg = `📝 [题目${currentQuestionNum}] 临时文本元素已更新: "${intermediateTextEl.textContent}"`;
                console.log(msg);
                if (window.addDebugLog) window.addDebugLog(msg, 'transcript');
            }
            if (emptyTextEl) {
                emptyTextEl.style.display = 'none';
            }
            // 有转写内容时显示提示（提示默认已显示，这里确保显示）
            if (autoAdvanceTip) {
                autoAdvanceTip.style.display = 'flex';
            }
            
            // 调试日志
            if (this.intermediateText) {
                const msg = `🔄 [题目${currentQuestionNum}] 显示临时文本: "${this.intermediateText}"`;
                console.log(msg);
                if (window.addDebugLog) window.addDebugLog(msg, 'transcript');
            }
            if (this.transcribedText) {
                const msg = `✅ [题目${currentQuestionNum}] 显示最终文本: "${this.transcribedText}"`;
                console.log(msg);
                if (window.addDebugLog) window.addDebugLog(msg, 'transcript');
            }
        } else {
            // 显示空状态
            if (transcriptText) transcriptText.style.display = 'none';
            if (intermediateTextEl) intermediateTextEl.style.display = 'none';
            if (emptyTextEl) emptyTextEl.style.display = 'block';
            // 提示始终显示，不隐藏
            if (autoAdvanceTip) {
                autoAdvanceTip.style.display = 'flex';
            }
            const msg = `📭 [题目${currentQuestionNum}] 显示空状态提示`;
            console.log(msg);
            if (window.addDebugLog) window.addDebugLog(msg, 'info');
        }

        // 更新录音状态（不控制容器显示）
        this.updateRecordingStatus('recording', this.transcribedText || this.intermediateText || '');
        
        // 再次确保容器显示（防止被其他代码隐藏）
        if (transcriptionContainer) {
            const finalDisplay = transcriptionContainer.style.display;
            if (finalDisplay === 'none') {
                transcriptionContainer.style.display = 'flex';
                const msg = `⚠️ [题目${currentQuestionNum}] 容器被隐藏，强制显示`;
                console.warn(msg);
                if (window.addDebugLog) window.addDebugLog(msg, 'warning');
            }
        }

        this.syncTranscriptionVisualMode();
    }

    // 更新转录文本显示（保持向后兼容）
    updateTranscript(transcript) {
        if (!transcript || transcript.trim() === '') {
            return;
        }

        // 追加到缓冲区
        this.transcriptBuffer += (this.transcriptBuffer ? ' ' : '') + transcript.trim();
        this.transcribedText = this.transcriptBuffer;
        this.accumulatedText = this.transcriptBuffer;

        // 更新显示
        this.updateTranscriptDisplay();

        console.log('📝 转录更新:', this.transcriptBuffer);
    }

    _resolveRecordingStoppedIfWaiting() {
        if (this._recordingStoppedResolver) {
            const r = this._recordingStoppedResolver;
            this._recordingStoppedResolver = null;
            try {
                r();
            } catch (e) {
                console.error('_recordingStoppedResolver 执行异常:', e);
            }
        }
    }

    /**
     * 在已调用 wsASRClient.stopRecording() 且返回 true 后，等待服务端 recording_stopped。
     * @returns {Promise<boolean>} true 表示收到信号，false 表示超时
     */
    _waitForWsRecordingStopped(timeoutMs = 15000) {
        return new Promise((resolve) => {
            const timer = setTimeout(() => {
                if (this._recordingStoppedResolver) {
                    this._recordingStoppedResolver = null;
                }
                console.warn('等待服务端 recording_stopped 超时（仍将尝试完成面试）');
                resolve(false);
            }, timeoutMs);
            this._recordingStoppedResolver = () => {
                clearTimeout(timer);
                this._recordingStoppedResolver = null;
                resolve(true);
            };
        });
    }

    /**
     * 面试结束统一收尾：先发 stop_recording 并等待服务端落库，再停本地录音、调用 HTTP complete（仅一次），最后 UI 与清理。
     */
    async handleInterviewCompleted(data = {}) {
        // 停止倒计时
        this.stopInterviewCountdown();

        this.isRecording = false;
        this.stopVolumeVisualizerLoop();
        this.stopVolumeMonitoring();

        // 流式 ASR：先发 stop_recording，再等待服务端 recording_stopped（确保尾段识别已写入 DB）
        if (this.wsASRClient) {
            const sent = this.wsASRClient.stopRecording();
            if (sent) {
                await this._waitForWsRecordingStopped(15000);
            }
        }

        await this.stopAllRecording();

        if (this.fullRecordingChunks.length > 0) {
            console.log('保存全程录音...');
            await this.saveFullRecording();
        }

        const sessionId = this.currentSession && this.currentSession.session_id;
        if (!sessionId) {
            console.warn('⚠️ 无法完成面试: session_id不存在');
            this.showError('会话无效，无法完成面试');
            return;
        }

        let result;
        try {
            console.log('🔄 调用完成面试接口: session_id=' + sessionId);
            result = await window.API.interview.completeInterview(sessionId);
        } catch (error) {
            console.error('❌ 完成面试请求失败:', error);
            this.showError('完成面试失败: ' + (error.message || String(error)));
            return;
        }

        if (!result.success) {
            console.error('❌ 完成面试失败: 服务器返回失败状态', result);
            this.showError(result.message || '完成面试失败');
            return;
        }

        if (result.data && result.data.evaluation_result) {
            console.log('📊 评估结果:', result.data.evaluation_result);
        }
        if (result.data && typeof result.data.total_questions === 'number') {
            console.log(`面试完成！共回答 ${result.data.total_questions} 个问题`);
        }

        this.updateStatus('已完成', 'completed');
        this.updateButtonStates('completed');

        // 获取面试总时长（展示用）
        let interviewDuration = '';
        try {
            if (data.durationMinutes !== undefined && data.durationMinutes !== null) {
                const minutes = Math.floor(data.durationMinutes);
                const seconds = Math.round((data.durationMinutes - minutes) * 60);
                if (minutes > 0) {
                    interviewDuration = `${minutes}分钟${seconds > 0 ? seconds + '秒' : ''}`;
                } else {
                    interviewDuration = `${seconds}秒`;
                }
            } else {
                const usedTime = this.INTERVIEW_DURATION - this.interviewRemainingTime;
                const hours = Math.floor(usedTime / 3600);
                const minutes = Math.floor((usedTime % 3600) / 60);
                const seconds = usedTime % 60;

                if (hours > 0) {
                    interviewDuration = `${hours}小时${minutes}分钟${seconds}秒`;
                } else if (minutes > 0) {
                    interviewDuration = `${minutes}分钟${seconds}秒`;
                } else {
                    interviewDuration = `${seconds}秒`;
                }
            }
        } catch (error) {
            console.error('获取面试时长失败:', error);
            interviewDuration = '';
        }

        this.lastInterviewDuration = interviewDuration;

        this.showInterviewCompletionDialog(interviewDuration);
        this.addBotMessage('面试完成！感谢您的参与。');

        try {
            interviewStarted = false;
        } catch (e) { /* ignore */ }
        this.interviewStarted = false;
        try {
            window.__interviewStartedInThisPage = false;
        } catch (e) { /* ignore */ }
        console.log('🔄 面试完成，重置状态标志');

        this.cleanup();

        try {
            localStorage.removeItem('isLoggedIn');
            localStorage.removeItem('userData');
            localStorage.removeItem('invitationData');
            console.log('🧹 已清理登录状态和会话数据');
        } catch (e) {
            console.warn('清理 localStorage 失败:', e);
        }
    }
    
    // 显示面试完成对话框
    showInterviewCompletionDialog(duration) {
        // 判断是否为移动端：根据页面URL或容器类名
        const isMobilePage = window.location.pathname.includes('mobile-interview');
        const hasMobileContainer = document.querySelector('.mobile-interview-container') !== null;
        // 电脑端：访问 desktop-interview 或 interview（非mobile-interview）
        const isDesktopPage = window.location.pathname.includes('desktop-interview') || 
                              (window.location.pathname.includes('interview') && !window.location.pathname.includes('mobile-interview'));
        const hasDesktopContainer = document.querySelector('.interview-container:not(.mobile-interview-container)') !== null;
        const isMobile = (isMobilePage || hasMobileContainer) && !isDesktopPage && !hasDesktopContainer;
        
        // 创建对话框HTML（移动端优化）
        const dialogHTML = `
            <div id="completionDialog" style="position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.7); z-index: 10000; display: flex; align-items: center; justify-content: center; padding: ${isMobile ? '20px' : '0'};">
                <div style="background: white; padding: ${isMobile ? '30px 20px' : '40px 30px'}; border-radius: ${isMobile ? '15px' : '12px'}; max-width: ${isMobile ? '100%' : '500px'}; width: ${isMobile ? '100%' : 'auto'}; text-align: center; box-shadow: 0 4px 20px rgba(0,0,0,0.3); margin: ${isMobile ? '0' : 'auto'};">
                    <div style="font-size: ${isMobile ? '48px' : '60px'}; color: #28a745; margin-bottom: ${isMobile ? '15px' : '20px'};">
                        <i class="fas fa-check-circle"></i>
                    </div>
                    <h2 style="color: #28a745; margin-bottom: ${isMobile ? '15px' : '20px'}; font-size: ${isMobile ? '22px' : '28px'}; font-weight: 600;">
                        面试完成
                    </h2>
                    <p style="font-size: ${isMobile ? '16px' : '18px'}; margin-bottom: ${isMobile ? '15px' : '20px'}; color: #333; line-height: 1.6;">
                        感谢您的参与！
                    </p>
                    ${duration ? `
                        <div style="background: #f8f9fa; padding: ${isMobile ? '15px' : '20px'}; border-radius: 8px; margin-bottom: ${isMobile ? '20px' : '25px'}; border-left: 4px solid #28a745;">
                            <p style="font-size: ${isMobile ? '14px' : '16px'}; color: #666; margin-bottom: 5px;">本次面试时长</p>
                            <p style="font-size: ${isMobile ? '24px' : '32px'}; color: #28a745; font-weight: 700; margin: 0;">${duration}</p>
                        </div>
                    ` : ''}
                    <button onclick="document.getElementById('completionDialog').remove(); window.location.href='/login';" 
                            style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border: none; padding: ${isMobile ? '14px 28px' : '12px 24px'}; border-radius: 8px; font-size: ${isMobile ? '16px' : '14px'}; font-weight: 500; cursor: pointer; width: ${isMobile ? '100%' : 'auto'}; transition: all 0.3s ease; box-shadow: 0 2px 8px rgba(102, 126, 234, 0.3);"
                            onmouseover="this.style.transform='translateY(-2px)'; this.style.boxShadow='0 4px 12px rgba(102, 126, 234, 0.4)';"
                            onmouseout="this.style.transform='translateY(0)'; this.style.boxShadow='0 2px 8px rgba(102, 126, 234, 0.3)';">
                        返回登录页面
                    </button>
                </div>
            </div>
        `;
        
        // 移除旧的对话框（如果存在）
        const oldDialog = document.getElementById('completionDialog');
        if (oldDialog) {
            oldDialog.remove();
        }
        
        // 添加新对话框
        document.body.insertAdjacentHTML('beforeend', dialogHTML);
        
        // 确保对话框在最上层
        const dialog = document.getElementById('completionDialog');
        if (dialog) {
            dialog.style.zIndex = '99999';
        }
    }

    // 清空聊天消息（切换题目时使用）
    clearChatMessages() {
        const chatMessages = document.getElementById('chatMessages');
        if (chatMessages) {
            // 保留系统消息，清除题目和回答
            const systemMessages = chatMessages.querySelectorAll('.message.system');
            chatMessages.innerHTML = '';
            // 重新添加系统消息
            systemMessages.forEach(msg => {
                chatMessages.appendChild(msg.cloneNode(true));
            });
            console.log('✅ 已清空聊天消息（保留系统消息）');
        }
        this._lastDisplayedQuestionBubbleId = null;
        this._lastDisplayedFollowUpParentId = null;
    }

    // 清空转写显示
    clearTranscriptDisplay() {
        const transcriptionContainer = document.getElementById('transcriptionContainer');
        const transcriptText = document.getElementById('transcriptText');
        const intermediateTextEl = document.getElementById('intermediateText');
        const emptyTextEl = document.getElementById('emptyText');
        const autoAdvanceTip = document.getElementById('autoAdvanceTip');
        
        // 清空文本
        this.transcriptBuffer = '';
        this.intermediateText = '';
        this.transcribedText = '';
        this.accumulatedText = '';
        
        if (transcriptText) transcriptText.textContent = '';
        if (intermediateTextEl) intermediateTextEl.textContent = '';
        
        // 显示空状态
        if (transcriptionContainer) {
            transcriptionContainer.style.display = 'flex';
            if (emptyTextEl) emptyTextEl.style.display = 'block';
            if (transcriptText) transcriptText.style.display = 'none';
            if (intermediateTextEl) intermediateTextEl.style.display = 'none';
            // 提示始终显示，不隐藏
            if (autoAdvanceTip) {
                autoAdvanceTip.style.display = 'flex';
            }
        }

        this.syncTranscriptionVisualMode();
    }
    
    // 清理资源（页面卸载时调用）
    async cleanup() {
        try {
            // 停止新音频系统
            if (this.audioManager && this.audioManager.isRecording) {
                this.audioManager.stopRecording();
            }
            
            // 断开WebSocket
            if (this.wsASRClient) {
                this.wsASRClient.disconnect();
            }
            
            // 销毁AudioManager
            if (this.audioManager) {
                await this.audioManager.destroy();
            }
            
            console.log('✅ 资源清理完成');
        } catch (error) {
            console.error('❌ 资源清理失败:', error);
        }
    }

    // 开始静默检测
    startSilenceDetection() {
        this.silenceTimeout = setInterval(() => {
            const now = Date.now();
            const silenceDuration = now - this.lastAudioTime;

            if (silenceDuration >= 3000) { // 3秒无内容
                console.log('检测到3秒静默，自动提交答案');
                this.autoSubmitAnswer();
            }
        }, 1000);
    }

    // 自动提交答案
    async autoSubmitAnswer() {
        if (this.silenceTimeout) {
            clearInterval(this.silenceTimeout);
            this.silenceTimeout = null;
        }

        if (this.mediaRecorder && this.mediaRecorder.state === 'recording') {
            this.mediaRecorder.stop();
        }

        // 使用转录文本作为答案
        const answerText = this.transcriptBuffer || '语音回答';

        // 显示用户回答
        this.addUserMessage(answerText);

        // 进行LLM评分
        await this.performLLMEvaluation(answerText);

        // 获取下一问题或完成面试
        await this.handleAnswerSubmission(answerText);
    }

    // 执行LLM评分
    async performLLMEvaluation(answerText) {
        try {
            if (!this.currentQuestion || !this.currentQuestion.evaluation_points) {
                console.warn('当前问题没有评估要点，跳过LLM评分');
                return;
            }

            console.log('开始LLM评分...');

            const response = await fetch('/api/v1/evaluation/llm-score', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    question_id: this.currentQuestion.question_id,
                    evaluation_points: this.currentQuestion.evaluation_points,
                    answer_text: answerText,
                    session_id: this.currentSession?.session_id
                })
            });

            if (response.ok) {
                const result = await response.json();
                if (result.success) {
                    console.log('LLM评分完成:', result.data);

                    // 显示评分结果
                    this.displayEvaluationResult(result.data);
                } else {
                    console.warn('LLM评分失败:', result.message);
                }
            } else {
                console.error('LLM评分请求失败:', response.status);
            }
        } catch (error) {
            console.error('LLM评分过程中出错:', error);
        }
    }

    // 显示评分结果
    displayEvaluationResult(evaluationData) {
        const score = evaluationData.score || 0;
        const feedback = evaluationData.feedback || '评分完成';

        // 添加评分消息
        this.addBotMessage(`评分结果：${score}分\n反馈：${feedback}`, 'evaluation');
    }

    // 处理答案提交
    async handleAnswerSubmission(answerText) {
        try {
            // 提交文本回答到后端
            const result = await window.API.interview.submitTextAnswer(
                this.currentSession.session_id,
                this.currentQuestion.question_id,
                answerText
            );

            if (result.success) {
                if (result.data.is_completed) {
                    // 面试完成
                    this.updateStatus('已完成', 'completed');
                    this.updateButtonStates('completed');
                    this.addBotMessage('面试完成！感谢您的参与。');
                    this.showSuccess('面试完成！感谢您的参与');
                } else {
                    // 获取下一问题
                    if (result.data.next_question) {
                        this.updateCurrentQuestion(result.data.next_question);
                        this.updateProgress(result.data.current_index, result.data.total_questions);

                        // 重置状态，准备下一题
                        this.updateButtonStates('active');
                    }
                }
            } else {
                throw new Error(result.message || '提交答案失败');
            }
        } catch (error) {
            console.error('提交答案失败:', error);
            this.showError('提交答案失败: ' + error.message);
            this.updateButtonStates('active');
        }
    }

    async toggleVoiceRecording() {
        if (this.mediaRecorder && this.mediaRecorder.state === 'recording') {
            await this.stopRecording();
        } else {
            await this.startRecording();
        }
    }

    async startRecording() {
        try {
            if (!this.currentQuestion) {
                this.showError('请先获取问题');
                return;
            }

            // 标记为非实时模式
            this.isRealTimeMode = false;

            this.showLoading('准备录制', '正在初始化录音设备');

            // 在录音前再次检查麦克风权限
            console.log('录音前检查麦克风权限...');
            if (navigator.permissions) {
                try {
                    const permissionStatus = await navigator.permissions.query({ name: 'microphone' });
                    if (permissionStatus.state === 'denied') {
                        this.hideLoading();
                        this.showError('麦克风权限已被拒绝，无法开始录音。请在浏览器设置中允许访问麦克风。');
                        return;
                    }
                } catch (error) {
                    console.warn('无法检查权限状态，继续尝试录音:', error);
                }
            }

            // 检查浏览器支持
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                this.hideLoading();
                this.showError('您的浏览器不支持麦克风访问功能。建议使用Chrome、Firefox、Safari等现代浏览器，并确保页面通过HTTPS访问。');
                return;
            }

            // 获取音频流（优化移动设备兼容性）
            const isMobile = /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);
            const audioConstraints = isMobile ? {
                audio: {
                    echoCancellation: true,
                    noiseSuppression: true,
                    autoGainControl: true,
                    sampleRate: 44100,
                    channelCount: 1
                }
            } : { audio: true };

            const stream = await navigator.mediaDevices.getUserMedia(audioConstraints);

            // 选择最佳录音格式并创建MediaRecorder
            const mimeType = this.selectBestMimeType();
            if (!mimeType) {
                throw new Error('浏览器不支持任何音频录制格式，请升级浏览器');
            }

            const recorderOptions = { mimeType: mimeType };
            this.mediaRecorder = new MediaRecorder(stream, recorderOptions);

            // 保存实际使用的MIME类型，用于后续创建Blob
            this.currentMimeType = mimeType;

            this.audioChunks = [];

            this.mediaRecorder.ondataavailable = (event) => {
                if (event.data.size > 0) {
                    this.audioChunks.push(event.data);
                }
            };

            this.mediaRecorder.onstop = () => {
                // 停止所有音频轨道
                stream.getTracks().forEach(track => track.stop());
            };

            this.mediaRecorder.start();

            // 更新UI状态
            const voiceRecordBtn = document.getElementById('voiceRecordBtn');
            const recordingStatus = document.getElementById('recordingStatus');

            if (voiceRecordBtn) {
                voiceRecordBtn.classList.add('recording');
            }
            if (recordingStatus) {
                recordingStatus.style.display = 'flex';
            }

            // 开始计时
            this.recordingDuration = 0;
            this.recordingTimer = setInterval(() => {
                this.recordingDuration++;
            }, 1000);

            this.hideLoading();

        } catch (error) {
            console.error('开始录制失败:', error);
            this.hideLoading();
            this.showError('开始录制失败: ' + error.message);
        }
    }

    async stopRecording() {
        try {
            // 停止录音时长监控
            if (this.recordingDurationInterval) {
                clearInterval(this.recordingDurationInterval);
                this.recordingDurationInterval = null;
            }

            // 隐藏调试信息
            this.hideDebugInfo();

            // 检查录音时长是否足够
            const finalDuration = this.recordingDuration || 0;
            if (finalDuration < 1.0) {
                this.showError(`录音时长过短 (${finalDuration.toFixed(1)}秒)，请至少录音1秒以上`);
                this.updateButtonStates('active');
                return;
            } else if (finalDuration < 2.0) {
                console.warn(`录音时长较短: ${finalDuration.toFixed(1)}秒`);
            } else {
                console.log(`录音时长正常: ${finalDuration.toFixed(1)}秒`);
            }

            // 如果使用新音频系统，停止它
            if (this.useNewAudioSystem && this.audioManager) {
                await this.stopNewAudioSystem();
                return;
            }
            
            // 如果使用RecordRTC，停止它
            if (!this.useNewAudioSystem && this.recordRTC) {
                await this.stopRecordRTCRecording();
                return;
            }

            // 旧的MediaRecorder系统
            if (!this.mediaRecorder || this.mediaRecorder.state !== 'recording') {
                return;
            }

            this.mediaRecorder.stop();

            // 更新UI状态
            const voiceRecordBtn = document.getElementById('voiceRecordBtn');
            const recordingStatus = document.getElementById('recordingStatus');

            if (voiceRecordBtn) {
                voiceRecordBtn.classList.remove('recording');
            }
            if (recordingStatus) {
                recordingStatus.style.display = 'none';
            }

            // 清除计时器
            if (this.recordingTimer) {
                clearInterval(this.recordingTimer);
                this.recordingTimer = null;
            }

            // 等待录制停止
            setTimeout(async () => {
                await this.submitVoiceAnswer();
            }, 500);

        } catch (error) {
            console.error('停止录制失败:', error);
            this.showError('停止录制失败');
        }
    }
    
    // 停止新的音频系统（仅停止录音，不断开WebSocket连接）
    async stopNewAudioSystem() {
        try {
            console.log('🛑 停止新音频系统（保持WebSocket连接）...');

            this.stopVolumeVisualizerLoop();
            this.stopVolumeMonitoring();

            if (this.wsASRClient) {
                this.wsASRClient.stopRecording();
            }

            // 停止录音，但保持WebSocket连接不断开
            if (this.audioManager) {
                this.audioManager.stopRecording();
                console.log('✅ AudioManager已停止');
            }

            // 注意：不主动断开WebSocket连接，整个面试过程保持连接
            // WebSocket连接将在页面卸载时由cleanup()方法断开
            console.log('ℹ️ WebSocket连接保持不断开，继续可用');

            // 更新状态
            this.isRealTimeMode = false;
            this.isRecording = false;
            this.updateButtonStates('active');

            // 提交答案（使用转录的文本）
            if (this.transcriptBuffer) {
                await this.submitTextAnswerFromTranscript();
            }

            this.syncTranscriptionVisualMode();
            
            console.log('✅ 新音频系统已停止');
            
        } catch (error) {
            console.error('❌ 停止新音频系统失败:', error);
            throw error;
        }
    }
    
    // 停止RecordRTC录音（参考旧代码）
    async stopRecordRTCRecording() {
        try {
            console.log('🛑 停止RecordRTC录音...');

            this.stopVolumeVisualizerLoop();
            this.stopVolumeMonitoring();
            
            // 停止前端发送定时器并清空缓冲队列
            if (this.recordRTCSendTimer) {
                clearInterval(this.recordRTCSendTimer);
                this.recordRTCSendTimer = null;
            }
            this.recordRTCAudioQueue = [];
            
            if (this.recordRTC) {
                // 停止录音
                this.recordRTC.stopRecording(() => {
                    console.log('[录音] 已停止发送音频数据');
                });
                
                // 发送停止录制命令
                if (this.wsASRClient && this.wsASRClient.isConnected) {
                    this.wsASRClient.sendControlMessage('stop_recording', {
                        timestamp: Date.now()
                    });
                }
                
                // 释放媒体流
                if (this.stream) {
                    this.stream.getTracks().forEach(track => track.stop());
                    this.stream = null;
                }
                
                this.recordRTC = null;
            }

            this.isRecording = false;

            this.syncTranscriptionVisualMode();
            
            console.log('✅ RecordRTC录音已停止');
        } catch (error) {
            console.error('❌ 停止RecordRTC录音失败:', error);
            throw error;
        }
    }
    
    // 从转录文本提交答案
    async submitTextAnswerFromTranscript() {
        try {
            if (!this.currentQuestion || !this.transcriptBuffer.trim()) {
                console.warn('⚠️ 没有转录文本或当前问题');
                return;
            }
            
            console.log('📤 提交文本答案:', this.transcriptBuffer);
            
            // 显示用户回答
            this.addUserMessage(this.transcriptBuffer);
            
            // 提交答案
            const result = await window.API.interview.submitTextAnswer(
                this.currentSession.session_id,
                this.currentQuestion.question_id,
                this.transcriptBuffer
            );
            
            if (result.success) {
                if (result.data.is_completed) {
                    // 面试完成
                    this.updateStatus('已完成', 'completed');
                    this.updateButtonStates('completed');
                    this.addBotMessage('面试完成！感谢您的参与。');
                    this.showSuccess('面试完成！感谢您的参与');
                } else {
                    // 获取下一问题
                    if (result.data.next_question) {
                        this.updateCurrentQuestion(result.data.next_question);
                        this.updateProgress(result.data.current_index, result.data.total_questions);
                        this.enableInputControls();
                    }
                }
            }
            
            // 清空转录缓冲区
            this.transcriptBuffer = '';
            this.clearTranscriptDisplay();
            
        } catch (error) {
            console.error('❌ 提交文本答案失败:', error);
            this.showError('提交答案失败: ' + error.message);
        }
    }

    async submitVoiceAnswer() {
        try {
            if (this.audioChunks.length === 0) {
                this.showError('没有录制到音频');
                return;
            }

            this.showLoading('处理回答', '正在处理语音回答');

            // 创建音频Blob，使用实际录制的格式
            const audioBlob = new Blob(this.audioChunks, { type: this.currentMimeType || 'audio/webm' });

            // 转换为Base64
            const reader = new FileReader();
            reader.onloadend = async () => {
                const base64Audio = reader.result.split(',')[1];

                try {
                    // 提交语音回答
                    const result = await window.API.interview.submitVoiceAnswer(
                        this.currentSession.session_id,
                        base64Audio
                    );

                    if (result.success) {
                        // 显示用户回答（ASR结果）
                        const answerText = result.data.asr_result?.text || '语音回答';
                        this.addUserMessage(answerText);

                        if (result.data.is_completed) {
                            // 面试完成
                            this.updateStatus('已完成', 'completed');
                            this.updateButtonStates('completed');
                            this.addBotMessage('面试完成！感谢您的参与。');
                            this.showSuccess('面试完成！感谢您的参与');
                        } else {
                            // 获取下一问题
                            if (result.data.next_question) {
                                this.updateCurrentQuestion(result.data.next_question);
                                this.updateProgress(result.data.current_index, result.data.total_questions);

                                // 暂时屏蔽语音播放，直接启用输入控件
                                console.log('语音播放功能已暂时屏蔽，直接启用语音录制控件');
                                this.enableInputControls();
                            }
                        }
                    }

                    this.hideLoading();

                } catch (error) {
                    console.error('提交语音回答失败:', error);
                    this.hideLoading();
                    this.showError('提交语音回答失败: ' + error.message);
                }
            };

            reader.readAsDataURL(audioBlob);

        } catch (error) {
            console.error('提交语音回答失败:', error);
            this.hideLoading();
            this.showError('提交语音回答失败: ' + error.message);
        }
    }

    // ==================== 清理资源 ====================
    cleanup() {
        // 停止所有音频流
        if (this.stream) {
            this.stream.getTracks().forEach(track => track.stop());
        }

        // 停止所有录音器
        if (this.mediaRecorder && this.mediaRecorder.state === 'recording') {
            this.mediaRecorder.stop();
        }

        if (this.fullRecordingRecorder && this.fullRecordingRecorder.state === 'recording') {
            this.fullRecordingRecorder.stop();
        }

        if (this.currentQuestionRecorder && this.currentQuestionRecorder.state === 'recording') {
            this.currentQuestionRecorder.stop();
        }

        // 停止所有定时器
        if (this.recordingTimer) {
            clearInterval(this.recordingTimer);
        }

        if (this.silenceTimeout) {
            clearTimeout(this.silenceTimeout);
        }

        if (this.volumeCheckInterval) {
            clearInterval(this.volumeCheckInterval);
        }

        if (this.transcriptionInterval) {
            clearInterval(this.transcriptionInterval);
        }

        // 关闭Web Audio API上下文
        if (this.audioContext && this.audioContext.state !== 'closed') {
            this.audioContext.close();
        }

        // 重置状态
        this.audioContext = null;
        this.analyser = null;
    }
}

// 创建全局实例
let interviewManager;

if (typeof window.interviewManagerInitialized === 'undefined') {
    window.interviewManagerInitialized = true;

    try {
        console.log('🏗️ 尝试创建InterviewManager实例...');
        interviewManager = new InterviewManager();
        // 设置为全局变量，供HTML页面使用
        window.interviewManager = interviewManager;
        console.log('✅ InterviewManager实例创建成功');
    } catch (error) {
        console.error('❌ InterviewManager实例创建失败:', error);
        console.error('错误详情:', error.stack);
        interviewManager = null;
        window.interviewManager = null;
    }
} else {
    console.log('⚠️ InterviewManager已经初始化，跳过重复创建');
    // 如果已经初始化，使用已存在的实例
    interviewManager = window.interviewManager;
}

// 页面加载完成后初始化事件绑定
document.addEventListener('DOMContentLoaded', function() {
    console.log('📄 DOM内容加载完成，开始绑定事件...');
    if (interviewManager) {
        try {
            interviewManager.bindButtonEvents();
            console.log('✅ 按钮事件绑定完成');
        } catch (error) {
            console.error('❌ 按钮事件绑定失败:', error);
        }
    } else {
        console.error('❌ interviewManager未初始化，无法绑定事件');
    }
});

// window load事件（检查事件绑定状态）
window.addEventListener('load', function() {
    console.log('🏁 窗口加载完成');

    // 延迟检查，确保interviewManager有时间初始化
    setTimeout(() => {
        if (interviewManager) {
            if (interviewManager.eventsBound) {
                console.log('✅ 事件绑定状态正常');
            } else {
                console.warn('⚠️ 事件绑定标志未设置，可能存在问题');
                // 尝试手动绑定事件（兜底方案）
                if (typeof interviewManager.bindEventListeners === 'function') {
                    console.log('🔧 尝试手动绑定事件...');
                    interviewManager.bindEventListeners();
                }
            }
        } else {
            console.warn('⚠️ interviewManager未初始化');
        }
    }, 1000); // 延迟1秒检查
});

// 全局函数（供HTML调用）

function logout() {
    console.log('🚪 用户退出登录，开始清理资源...');

    // 清理面试相关的资源
    if (interviewManager) {
        try {
            // 停止当前录音
            if (interviewManager.mediaRecorder &&
                interviewManager.mediaRecorder.state === 'recording') {
                interviewManager.mediaRecorder.stop();
                console.log('✅ 录音已停止');
            }

            // 停止实时转录
            if (interviewManager.transcriptionInterval) {
                clearInterval(interviewManager.transcriptionInterval);
                interviewManager.transcriptionInterval = null;
                console.log('✅ 实时转录已停止');
            }

            // 停止静默检测
            if (interviewManager.silenceTimeout) {
                clearInterval(interviewManager.silenceTimeout);
                interviewManager.silenceTimeout = null;
                console.log('✅ 静默检测已停止');
            }

            // 停止媒体流
            if (interviewManager.stream) {
                interviewManager.stream.getTracks().forEach(track => track.stop());
                interviewManager.stream = null;
                console.log('✅ 媒体流已停止');
            }

        } catch (error) {
            console.error('❌ 清理面试资源时出错:', error);
        }
    }

    // 清除登录状态
    localStorage.removeItem('isLoggedIn');
    localStorage.removeItem('userData');
    localStorage.removeItem('invitationData');
    console.log('✅ 本地存储已清理');

    // 跳转到登录页面
    window.location.href = '/login';
}

function startCamera() {
    interviewManager.startCamera();
}

function stopCamera() {
    interviewManager.stopCamera();
}

function capturePhoto() {
    interviewManager.capturePhoto();
}

function uploadPhoto() {
    interviewManager.uploadPhoto();
}

// 测试版本的开始面试函数
function testStartInterview() {
    console.log('=== 测试开始面试 ===');

    // 检查基本条件
    const invitationData = localStorage.getItem('invitationData');
    console.log('localStorage.invitationData:', invitationData);

    if (!invitationData) {
        alert('错误：没有找到邀请数据！请先登录。');
        return;
    }

    try {
        const parsed = JSON.parse(invitationData);
        console.log('解析后的邀请数据:', parsed);

        if (!parsed.invitation_id) {
            alert('错误：邀请数据中没有invitation_id！');
            return;
        }

        console.log('邀请ID:', parsed.invitation_id);

        // 检查API是否可用
        if (!window.API || !window.API.interview || !window.API.interview.startInterview) {
            alert('错误：API不可用！');
            return;
        }

        console.log('API检查通过，开始调用...');

        // 调用原始函数
        startInterview();
    } catch (e) {
        console.error('解析邀请数据失败:', e);
        alert('错误：邀请数据格式错误！' + e.message);
    }
}

// 防止重复调用标志
let interviewStarted = false;

// 全局函数定义 - 确保随时可用
console.log('📝 定义window.startInterview函数...');
window.startInterview = function() {
    console.log('🎯 全局startInterview函数被调用');

    if (interviewStarted) {
        console.warn('⚠️ 面试已经开始，忽略重复调用');
        return;
    }

    if (!interviewManager) {
        console.error('❌ interviewManager未初始化');
        alert('系统初始化失败，请刷新页面重试');
        return;
    }

    console.log('🚀 开始新的面试流程');
    interviewStarted = true;
    interviewManager.startInterview();
};

console.log('📝 定义startInterview函数...');
function startInterview() {
    console.log('🔄 调用全局startInterview函数');
    return window.startInterview();
}

console.log('📝 定义window.finishAnswer函数...');
window.finishAnswer = function() {
    console.log('🎯 全局finishAnswer函数被调用');
    if (interviewManager) {
        return interviewManager.finishAnswer();
    } else {
        console.error('❌ interviewManager未初始化');
    }
};

function finishAnswer() {
    console.log('🔄 调用全局finishAnswer函数');
    return window.finishAnswer();
}

function pauseInterview() {
    if (!interviewManager) {
        console.error('❌ interviewManager未初始化');
        return;
    }
    interviewManager.pauseInterview();
}

function resumeInterview() {
    if (!interviewManager) {
        console.error('❌ interviewManager未初始化');
        return;
    }
    interviewManager.resumeInterview();
}

function completeInterview() {
    if (!interviewManager) {
        console.error('❌ interviewManager未初始化');
        return;
    }
    interviewManager.completeInterview();
}

function playCurrentQuestion() {
    interviewManager.playCurrentQuestion();
}

function startRecording() {
    interviewManager.startRecording();
}

function stopRecording() {
    interviewManager.stopRecording();
}

function forceSendAudio() {
    if (interviewManager) {
        interviewManager.forceSendAudio();
    } else {
        console.error('interviewManager未初始化');
    }
}

function submitTextAnswer() {
    interviewManager.submitTextAnswer();
}

function hideSuccessModal() {
    interviewManager.hideSuccessModal();
}

function hideErrorModal() {
    interviewManager.hideErrorModal();
}

function sendMessage() {
    interviewManager.sendMessage();
}

function toggleVoiceRecording() {
    interviewManager.toggleVoiceRecording();
}

// 全局函数定义 - 确保随时可用
window.startAnswer = function() {
    console.log('🎯 全局startAnswer函数被调用');
    if (!interviewManager) {
        console.error('❌ interviewManager未初始化');
        alert('系统初始化失败，请刷新页面重试');
        return;
    }
    console.log('调用interviewManager.startAnswer');
    interviewManager.startAnswer();
};

function startAnswer() {
    return window.startAnswer();
}

// 开始回答按钮点击：未录音时开始录音，录音中时下一题（与旧代码 VoiceInterviewRecorder 逻辑一致）
window.handleStartAnswerButtonClick = function() {
    if (!interviewManager) {
        console.error('❌ interviewManager未初始化');
        return;
    }
    if (interviewManager.isRecording) {
        finishAnswer();
    } else {
        startAnswer();
    }
};

function handleStartAnswerButtonClick() {
    return window.handleStartAnswerButtonClick && window.handleStartAnswerButtonClick();
}

console.log('🎉 interview.js 加载完成');
