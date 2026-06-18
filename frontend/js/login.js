// 登录页面功能
class LoginManager {
    constructor() {
        this.initializePage();
    }

    // 初始化页面
    initializePage() {
        this.bindEventListeners();
        this.checkAuthStatus();
        // 支持面试直达链接：/login?username=...&password=...（可选 &mobile=true），读取后从地址栏移除敏感参数
        this.consumeInterviewDeepLink();
    }

    /**
     * 从 URL 查询串或 hash 中读取候选人账号密码并自动登录。
     * 支持的查询参数名：username|user|u，password|pwd|p
     * hash 示例：#username=xxx&password=yyy
     * 密码中含 + 等特殊字符时，生成链接应对值使用 encodeURIComponent。
     */
    consumeInterviewDeepLink() {
        const pick = (params) => {
            const username =
                params.get('username') ||
                params.get('user') ||
                params.get('u');
            const password =
                params.get('password') ||
                params.get('pwd') ||
                params.get('p');
            return { username, password };
        };

        const searchParams = new URLSearchParams(window.location.search);
        let { username, password } = pick(searchParams);

        if (!username || !password) {
            const rawHash = (window.location.hash || '').replace(/^#/, '');
            if (rawHash) {
                const hashQuery = rawHash.includes('=')
                    ? (rawHash.startsWith('?') ? rawHash.slice(1) : rawHash)
                    : '';
                if (hashQuery) {
                    const hp = new URLSearchParams(hashQuery);
                    const fromHash = pick(hp);
                    username = username || fromHash.username;
                    password = password || fromHash.password;
                }
            }
        }

        if (!username || !password) {
            return;
        }

        const userEl = document.getElementById('username');
        const passEl = document.getElementById('password');
        if (userEl) userEl.value = username;
        if (passEl) passEl.value = password;

        if (searchParams.get('mobile') === 'true') {
            sessionStorage.setItem('deviceType', 'mobile');
        }

        this._stripCredentialParamsFromUrl();

        setTimeout(() => {
            this.handleLogin();
        }, 0);
    }

    _stripCredentialParamsFromUrl() {
        try {
            const url = new URL(window.location.href);
            const sensitiveKeys = ['username', 'user', 'u', 'password', 'pwd', 'p', 'auto'];
            sensitiveKeys.forEach((k) => url.searchParams.delete(k));
            const qs = url.searchParams.toString();
            const pathWithQuery = url.pathname + (qs ? `?${qs}` : '');
            // 不传 fragment 可一并清除 hash（避免凭据留在 hash 中）
            history.replaceState({}, '', pathWithQuery);
        } catch (e) {
            console.warn('清理登录链接参数失败:', e);
        }
    }

    // 绑定事件监听器
    bindEventListeners() {
        const loginForm = document.getElementById('loginForm');
        const loginBtn = document.getElementById('loginBtn');
        const confirmBtn = document.getElementById('confirmBtn');
        const cancelBtn = document.getElementById('cancelBtn');

        if (loginForm) {
            loginForm.addEventListener('submit', (e) => {
                e.preventDefault();
                this.handleLogin();
            });
        }

        // 回车键提交
        document.addEventListener('keypress', (e) => {
            if (e.key === 'Enter' && !loginBtn.disabled) {
                this.handleLogin();
            }
        });

        // 模态窗口按钮事件
        if (confirmBtn) {
            confirmBtn.addEventListener('click', () => {
                this.confirmLogin();
            });
        }

        if (cancelBtn) {
            cancelBtn.addEventListener('click', () => {
                this.cancelLogin();
            });
        }
    }

    // 检查认证状态
    async checkAuthStatus() {
        try {
            // 检查是否已登录（这里可以根据需要添加token检查）
            const isLoggedIn = localStorage.getItem('isLoggedIn');
            const userData = localStorage.getItem('userData');

            // 不在登录页面检查认证状态，避免无限跳转
            // 用户需要手动登录进入面试页面
            // if (isLoggedIn === 'true' && userData) {
            //     // 已登录，直接跳转到面试页面
            //     window.location.href = '/interview';
            //     return;
            // }
        } catch (error) {
            console.error('检查认证状态失败:', error);
        }
    }

    // 处理登录
    async handleLogin() {
        const username = document.getElementById('username').value.trim();
        const password = document.getElementById('password').value;

        if (!username || !password) {
            this.showError('请输入用户名和密码');
            return;
        }

        this.setLoading(true);

        try {
            // 调用登录API
            const result = await window.API.auth.login(username, password);

            if (result.success) {
                // 登录成功
                this.showError('', false);

                // 构造用户对象
                const userData = {
                    username: result.invitation_data.invitation_id, // 使用邀请ID作为用户名
                    full_name: result.invitation_data.candidate_name,
                    user_type: 'candidate',
                    invitation_data: result.invitation_data
                };

                // 保存登录状态和用户数据到localStorage
                localStorage.setItem('isLoggedIn', 'true');
                localStorage.setItem('userData', JSON.stringify(userData));
                localStorage.setItem('invitationData', JSON.stringify(result.invitation_data));

                // 登录成功，直接跳转到面试页面（取消确认步骤）
                this.showSuccess('登录成功，正在进入面试...');

                // 检测来源页面，决定跳转到移动端还是电脑端
                const referrer = document.referrer;
                const isFromMobile = referrer.includes('mobile-interview') || 
                                     window.location.search.includes('mobile=true') ||
                                     /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);
                
                // 保存设备类型到sessionStorage，供后续页面使用
                if (isFromMobile) {
                    sessionStorage.setItem('deviceType', 'mobile');
                } else {
                    sessionStorage.setItem('deviceType', 'desktop');
                }

                // 1秒后自动跳转到对应的面试页面
                setTimeout(() => {
                    const targetUrl = isFromMobile ? '/mobile-interview' : '/interview';
                    console.log(`📱 检测到设备类型: ${isFromMobile ? '移动端' : '电脑端'}，跳转到: ${targetUrl}`);
                    window.location.href = targetUrl;
                }, 1000);

            } else {
                throw new Error(result.message || '登录失败');
            }

        } catch (error) {
            console.error('登录失败:', error);

            // 提供更详细的错误信息
            let errorMessage = error.message || '登录失败';
            if (error.message.includes('Failed to fetch') || error.message.includes('NetworkError')) {
                errorMessage = '网络连接失败，请检查服务器是否运行或网络连接是否正常。\n\n错误详情: ' + error.message;
            } else if (error.message.includes('Connection refused')) {
                errorMessage = '无法连接到服务器，请确保VoiceBridge服务正在运行。\n\n服务器地址: http://localhost:8002';
            }

            // 显示登录失败模态窗口
            this.showModal('登录失败', errorMessage, false);
        } finally {
            this.setLoading(false);
        }
    }

    // 显示/隐藏加载状态
    setLoading(loading) {
        const loginBtn = document.getElementById('loginBtn');
        const loadingSpinner = document.getElementById('loadingSpinner');

        if (loading) {
            loginBtn.disabled = true;
            loginBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 登录中...';
            if (loadingSpinner) loadingSpinner.style.display = 'block';
        } else {
            loginBtn.disabled = false;
            loginBtn.innerHTML = '<i class="fas fa-sign-in-alt"></i> 登录';
            if (loadingSpinner) loadingSpinner.style.display = 'none';
        }
    }

    // 显示错误信息
    showError(message, show = true) {
        const errorDiv = document.getElementById('errorMessage');

        if (show && message) {
            errorDiv.textContent = message;
            errorDiv.style.display = 'block';
        } else {
            errorDiv.style.display = 'none';
        }
    }

    // 显示成功信息
    showSuccess(message, duration = 2000) {
        return new Promise((resolve) => {
            const successDiv = document.getElementById('successMessage');

            if (successDiv && message) {
                successDiv.textContent = message;
                successDiv.style.display = 'block';

                setTimeout(() => {
                    successDiv.style.display = 'none';
                    resolve();
                }, duration);
            } else {
                resolve();
            }
        });
    }

    // 显示确认窗口
    showModal(title, message, isSuccess = true) {
        const confirmContainer = document.getElementById('confirmContainer');
        const confirmTitle = document.getElementById('confirmTitle');
        const confirmMessage = document.getElementById('confirmMessage');
        const confirmBtn = document.getElementById('confirmBtn');
        const cancelBtn = document.getElementById('cancelBtn');

        if (confirmTitle) confirmTitle.textContent = title;
        if (confirmMessage) confirmMessage.innerHTML = message;

        // 根据成功还是失败显示不同的按钮
        if (isSuccess) {
            confirmBtn.style.display = 'inline-block';
            cancelBtn.style.display = 'inline-block';
            cancelBtn.textContent = '取消';
        } else {
            confirmBtn.style.display = 'none';
            cancelBtn.style.display = 'inline-block';
            cancelBtn.textContent = '确定';
        }

        if (confirmContainer) {
            confirmContainer.style.display = 'flex';
        }
    }

    // 隐藏确认窗口
    hideModal() {
        const confirmContainer = document.getElementById('confirmContainer');
        if (confirmContainer) {
            confirmContainer.style.display = 'none';
        }
    }

    // 确认登录
    confirmLogin() {
        this.hideModal();
        // 检测设备类型，决定跳转到移动端还是电脑端
        const deviceType = sessionStorage.getItem('deviceType');
        const isMobile = deviceType === 'mobile' || 
                        /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);
        const targetUrl = isMobile ? '/mobile-interview' : '/interview';
        console.log(`📱 确认登录，跳转到: ${targetUrl}`);
        window.location.href = targetUrl;
    }

    // 取消登录
    cancelLogin() {
        this.hideModal();
        // 清除可能已保存的数据
        localStorage.removeItem('isLoggedIn');
        localStorage.removeItem('userData');
        localStorage.removeItem('invitationData');
    }
}

// 创建登录管理器实例
const loginManager = new LoginManager();