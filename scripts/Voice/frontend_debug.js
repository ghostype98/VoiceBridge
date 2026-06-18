// ============================================================================
// 前端调试代码 - 请在浏览器控制台执行
// ============================================================================

console.log('=== 🔍 面试系统调试信息 ===');
console.log('');

// 1. 检查InterviewManager实例
console.log('【1】InterviewManager实例:');
console.log('  存在:', !!window.interviewManager);
if (window.interviewManager) {
    console.log('  当前题目索引:', window.interviewManager.currentQuestionIndex);
    console.log('  总题目数:', window.interviewManager.totalQuestions);
    console.log('  基础题数:', window.interviewManager.basicQuestions?.length);
    console.log('  专业题数:', window.interviewManager.professionalQuestions?.length);
    console.log('  所有题目数:', window.interviewManager.allQuestions?.length);
}
console.log('');

// 2. 检查题目计数器元素
console.log('【2】题目计数器元素:');
const counter = document.querySelector('.question-counter');
console.log('  元素存在:', !!counter);
if (counter) {
    console.log('  当前文本:', counter.textContent);
    console.log('  父元素:', counter.parentElement?.className);
    console.log('  是否可见:', counter.offsetParent !== null);
}
console.log('');

// 3. 检查进度条元素
console.log('【3】进度条元素:');
const progressFill = document.getElementById('progressFill');
const progressPercent = document.getElementById('progressPercent');
const progressText = document.getElementById('progressText');
console.log('  progressFill:', !!progressFill, progressFill?.style.width);
console.log('  progressPercent:', !!progressPercent, progressPercent?.textContent);
console.log('  progressText:', !!progressText, progressText?.textContent);
console.log('');

// 4. 检查updateProgress函数
console.log('【4】updateProgress函数:');
if (window.interviewManager && typeof window.interviewManager.updateProgress === 'function') {
    console.log('  函数存在: ✅');
    
    // 手动调用一次看看效果
    console.log('  尝试手动调用updateProgress...');
    try {
        const current = window.interviewManager.currentQuestionIndex + 1;
        const total = window.interviewManager.totalQuestions;
        window.interviewManager.updateProgress(current, total);
        console.log('  ✅ 调用成功');
        console.log('  更新后的计数器文本:', document.querySelector('.question-counter')?.textContent);
    } catch (e) {
        console.log('  ❌ 调用失败:', e.message);
    }
} else {
    console.log('  函数不存在: ❌');
}
console.log('');

// 5. 检查当前题目信息
console.log('【5】当前题目信息:');
if (window.interviewManager?.currentQuestion) {
    const q = window.interviewManager.currentQuestion;
    console.log('  题目ID:', q.question_id);
    console.log('  题目文本:', q.question_text?.substring(0, 50) + '...');
    console.log('  题目类型:', q.question_type);
}
console.log('');

// 6. 监听题目切换
console.log('【6】设置题目切换监听:');
if (window.interviewManager) {
    const originalSwitch = window.interviewManager._switch_question;
    if (originalSwitch) {
        window.interviewManager._switch_question = function(...args) {
            console.log('🔄 题目切换触发:', args);
            console.log('  切换前索引:', this.currentQuestionIndex);
            const result = originalSwitch.apply(this, args);
            console.log('  切换后索引:', this.currentQuestionIndex);
            console.log('  计数器文本:', document.querySelector('.question-counter')?.textContent);
            return result;
        };
        console.log('  ✅ 已设置监听');
    }
}

console.log('');
console.log('=== 调试信息输出完毕 ===');
console.log('');
console.log('💡 提示:');
console.log('  - 如果计数器不更新，请检查updateProgress是否被正确调用');
console.log('  - 可以手动执行: window.interviewManager.updateProgress(2, 15)');
console.log('  - 切换题目时会自动输出日志');

