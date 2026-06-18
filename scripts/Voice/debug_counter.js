// 问题1：定位题目计数器元素
console.log('=== 题目计数器诊断 ===');
console.log('当前题目索引:', window.interviewManager?.currentQuestionIndex);
console.log('总题目数:', window.interviewManager?.totalQuestions);

const counter = document.querySelector('.question-counter');
console.log('计数器元素:', counter);
console.log('计数器文本:', counter?.textContent);
console.log('计数器父元素:', counter?.parentElement);

// 查找所有可能的计数器元素
const allCounters = document.querySelectorAll('[class*="counter"], [class*="progress"]');
console.log('所有计数器相关元素:', allCounters);
allCounters.forEach((el, i) => {
    console.log(`  ${i}: ${el.className} - ${el.textContent}`);
});

// 检查 updateProgress 函数
console.log('updateProgress 函数:', window.interviewManager?.updateProgress);

// 手动调用测试
if (window.interviewManager) {
    console.log('手动调用 updateProgress(5, 15)...');
    window.interviewManager.updateProgress(5, 15);
    console.log('调用后计数器文本:', counter?.textContent);
}

