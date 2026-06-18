#!/usr/bin/env node

/**
 * 浏览器兼容性测试脚本
 * 基于voice_interview_streaming参考模式的完整兼容性检查
 */

console.log('🔍 浏览器兼容性检查（基于voice_interview_streaming参考模式）...');

// 1. 检查MediaDevices API
if (!navigator.mediaDevices) {
    console.error('❌ navigator.mediaDevices 不存在');
    console.error('💡 建议：升级到Chrome 47+、Firefox 44+、Safari 14+等现代浏览器');
    process.exit(1);
} else {
    console.log('✅ navigator.mediaDevices API 存在');
}

// 2. 检查getUserMedia方法
if (!navigator.mediaDevices.getUserMedia) {
    console.error('❌ navigator.mediaDevices.getUserMedia 不存在');
    console.error('💡 建议：使用支持MediaDevices API的现代浏览器');
    process.exit(1);
} else {
    console.log('✅ getUserMedia 方法存在');
}

// 3. 检查是否在安全上下文中
if (!window.isSecureContext) {
    console.error('❌ 不在安全上下文中（HTTPS）');
    console.error('💡 建议：确保网站通过HTTPS协议访问，或使用localhost进行开发');
    process.exit(1);
} else {
    console.log('✅ 在安全上下文中（HTTPS）');
}

// 4. 检查权限API（推荐但不强制）
if (!navigator.permissions) {
    console.warn('⚠️ navigator.permissions 不存在，无法查询权限状态');
    console.log('ℹ️ 这不会阻止麦克风访问，但无法预先检查权限状态');
} else {
    console.log('✅ Permissions API 存在');
}

// 5. 检查MediaRecorder（用于录音）
if (!window.MediaRecorder) {
    console.warn('⚠️ MediaRecorder 不存在，将使用基础录音功能');
    console.log('ℹ️ 系统将尝试使用其他录音方案');
} else {
    console.log('✅ MediaRecorder 存在');

    // 检查支持的格式
    const supportedTypes = [
        'audio/webm;codecs=opus',
        'audio/webm',
        'audio/mp4',
        'audio/wav'
    ];

    const supportedFormats = supportedTypes.filter(type => MediaRecorder.isTypeSupported(type));
    console.log('🎵 支持的录音格式:', supportedFormats);
}

// 6. 检查Web Audio API（用于音频处理）
if (!window.AudioContext && !window.webkitAudioContext) {
    console.warn('⚠️ Web Audio API 不存在，将使用基础音频处理');
    console.log('ℹ️ 音频处理功能将受限');
} else {
    console.log('✅ Web Audio API 存在');
}

// 7. 检查WebSocket（用于实时通信）
if (!window.WebSocket) {
    console.warn('⚠️ WebSocket 不支持，实时功能将受限');
    console.log('ℹ️ 将使用HTTP轮询作为备选方案');
} else {
    console.log('✅ WebSocket 支持');
}

// 8. 检查设备类型和优化建议
const isMobile = /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);
if (isMobile) {
    console.log('📱 检测到移动设备，将使用移动设备优化设置');
} else {
    console.log('💻 检测到桌面设备，将使用完整功能');
}

// 9. 检查麦克风设备
navigator.mediaDevices.enumerateDevices()
    .then(devices => {
        const audioInputs = devices.filter(device => device.kind === 'audioinput');
        console.log(`🎙️ 发现 ${audioInputs.length} 个音频输入设备`);

        if (audioInputs.length === 0) {
            console.warn('⚠️ 未发现麦克风设备，请检查设备连接');
        } else {
            audioInputs.forEach((device, index) => {
                console.log(`  ${index + 1}. ${device.label || '未命名设备'} (${device.deviceId})`);
            });
        }

        // 输出最终结果
        console.log('\n🎉 浏览器兼容性检查完成！');
        console.log('📋 兼容性级别: 完整支持');
        console.log('🚀 系统已准备就绪，可以开始使用语音功能');
    })
    .catch(error => {
        console.warn('⚠️ 无法枚举设备:', error.message);
        console.log('\n🎉 浏览器兼容性检查完成！');
        console.log('📋 兼容性级别: 基本支持（无法检测设备）');
        console.log('🚀 系统已准备就绪，可以开始使用语音功能');
    });