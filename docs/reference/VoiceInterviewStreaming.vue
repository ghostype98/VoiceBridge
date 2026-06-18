<template>
  <div class="streaming-interview">
    <!-- 顶部进度条 -->
    <div class="interview-progress-bar">
      <div class="progress-info">
        <span class="question-counter">第 {{ currentQuestionIndex + 1 }} / {{ totalQuestions }} 题</span>
        <el-progress
          :percentage="progressPercentage"
          :status="progressPercentage === 100 ? 'success' : undefined"
          :stroke-width="8"
        />
      </div>
      <!-- 面试倒计时 -->
      <div v-if="isRecording" class="interview-countdown">
        <el-alert
          :title="`剩余时间：${formatCountdown(remainingTime)}`"
          :type="remainingTime <= 300 ? 'error' : remainingTime <= 600 ? 'warning' : 'info'"
          :closable="false"
          show-icon
        >
          <template #default>
            <span class="countdown-text">{{ formatCountdown(remainingTime) }}</span>
          </template>
        </el-alert>
      </div>
    </div>

    <!-- 主面试区域 -->
    <div class="interview-main" :class="{ 'switching': isSwitching }">
      <!-- 加载状态 -->
      <div v-if="questions.length === 0" class="loading-state">
        <el-icon class="is-loading" :size="48">
          <Loading />
        </el-icon>
        <p>正在加载题目...</p>
      </div>
      
      <!-- 当前题目显示 -->
      <transition v-else name="question-fade" mode="out-in">
        <div v-if="currentQuestion || followUpQuestion" :key="(currentQuestion?.question_id || '') + (followUpQuestion || '')" class="question-container">
          <!-- 追问问题特别提示 -->
          <div v-if="followUpQuestion" class="follow-up-notice">
            <el-alert
              title="追问问题"
              type="warning"
              :closable="false"
              show-icon
            >
              <template #default>
                <p class="follow-up-hint">这是针对您刚才回答的追问问题，请详细说明：</p>
              </template>
            </el-alert>
          </div>
          
          <div v-if="currentQuestion" class="question-header">
            <el-tag :type="(currentQuestion.question_type === 'BASIC' || currentQuestion.question_type === 'BASIC_INFO') ? 'success' : 'warning'" size="large">
              {{ (currentQuestion.question_type === 'BASIC' || currentQuestion.question_type === 'BASIC_INFO') ? '基础题' : '专业题' }}
            </el-tag>
            <el-tag size="large">{{ currentQuestion.question_category }}</el-tag>
          </div>
          
          <!-- 显示追问问题或当前题目 -->
          <h1 class="question-text" :class="{ 'follow-up-question': followUpQuestion }">
            {{ followUpQuestion || currentQuestion?.question_text }}
          </h1>
          
          <!-- 录音状态指示器 -->
          <div class="recording-indicator" v-if="isRecording">
            <div class="wave-animation">
              <span></span>
              <span></span>
              <span></span>
              <span></span>
              <span></span>
            </div>
            <span class="recording-text">正在录音中...</span>
          </div>
        </div>
      </transition>

      <!-- 转写结果显示（参考老地址：始终显示，实时转写） -->
      <div class="transcription-container" v-if="isRecording">
        <div class="transcription-header">
          <h3>回答内容：</h3>
        </div>
        <div class="transcription-text">
          <div v-if="transcribedText || intermediateText">
            <!-- 最终文本（已确认的转写结果） -->
            <div v-if="transcribedText" class="final-text">{{ transcribedText }}</div>
            <!-- 临时文本（正在识别中，实时显示） -->
            <div v-if="intermediateText" class="intermediate-text">{{ intermediateText }}</div>
          </div>
          <div v-else class="empty-text">等待语音输入...</div>
        </div>
      </div>

      <!-- 静音倒计时提示 -->
      <transition name="countdown-fade">
        <div v-if="showSilenceCountdown" class="silence-countdown">
          <el-alert
            :title="`检测到您已停止说话，${silenceCountdown}秒后自动进入下一题`"
            type="warning"
            :closable="false"
            show-icon
          >
            <template #default>
              <el-button size="small" @click="cancelAutoAdvance">取消</el-button>
            </template>
          </el-alert>
        </div>
      </transition>

      <!-- 控制按钮：面试已结束（弹窗显示中）不再显示“开始面试” -->
      <div class="control-buttons">
        <el-button
          v-if="!isRecording && !showCompletionDialog"
          type="primary"
          size="large"
          @click="startInterview"
          :loading="isStarting"
        >
          <span>🎤</span>
          开始面试
        </el-button>
        
        <template v-else-if="isRecording">
          <el-button
            type="success"
            size="large"
            @click="manualNextQuestion"
            :disabled="isSwitching"
          >
            <span>⏭</span>
            回答完毕，下一题
          </el-button>
          
          <el-button
            type="danger"
            size="large"
            @click="stopInterview"
          >
            <span>⏹</span>
            结束面试
          </el-button>
        </template>
      </div>
    </div>

    <!-- 语音录制组件（隐藏但保持运行） -->
    <VoiceInterviewRecorder
      ref="recorderRef"
      :invitation-id="invitationId"
      :question-id="currentQuestion?.question_id"
      :websocket-url="websocketUrl"
      @text-update="handleTextUpdate"
      @evaluation-update="handleEvaluationUpdate"
      @error="handleError"
      @question-switched="handleQuestionSwitched"
      @follow-up-trigger="handleFollowUpTrigger"
      @next-question="handleNextQuestion"
      @interview-completed="handleInterviewCompleted"
      @silence-countdown="handleSilenceCountdown"
      style="display: none;"
    />

    <!-- 面试完成弹窗 -->
    <el-dialog
      v-model="showCompletionDialog"
      title=""
      width="600px"
      :close-on-click-modal="false"
      :close-on-press-escape="false"
      :show-close="false"
    >
      <div class="completion-content">
        <div class="completion-message">
          <el-icon class="success-icon" :size="64">
            <CircleCheck />
          </el-icon>
          <h2>面试已完成</h2>
          <p class="completion-text">感谢您的参与，面试结果将稍后通知您</p>
          <div class="duration-info" v-if="interviewDuration">
            <el-divider />
            <p class="duration-text">本次面试用时：<span class="duration-value">{{ interviewDuration }}</span></p>
          </div>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, computed, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Loading, CircleCheck } from '@element-plus/icons-vue'
import VoiceInterviewRecorder from '@/components/voice-interview/VoiceInterviewRecorder.vue'
import { voiceInterviewApi } from '@/api/modules/voice-interview'
import { DEBUG_CONFIG } from '@config'

// 路由
const route = useRoute()
const router = useRouter()

// Props
const invitationId = route.params.invitationId as string

// 状态
const questions = ref<any[]>([])
const currentQuestionIndex = ref(0)
const currentQuestion = computed(() => questions.value[currentQuestionIndex.value] || null)
const totalQuestions = computed(() => questions.value.length)
const progressPercentage = computed(() => {
  if (totalQuestions.value === 0) return 0
  return Math.round(((currentQuestionIndex.value + 1) / totalQuestions.value) * 100)
})

const isRecording = ref(false)
const isStarting = ref(false)
const isSwitching = ref(false)
const followUpQuestion = ref<string | null>(null)  // 当前追问问题
const transcribedText = ref('')
const intermediateText = ref('')  // 临时转写文本（正在识别中）
const latestEvaluation = ref<any>(null)
const recorderRef = ref()

// 调试开关：显示语音转文字结果（从统一配置文件读取）
const showTranscription = ref(DEBUG_CONFIG.showTranscription)

// 静音倒计时
const showSilenceCountdown = ref(false)
const silenceCountdown = ref(3)
let countdownTimer: number | null = null

// 面试倒计时（40分钟 = 2400秒，基本信息10分钟+专业技能30分钟）
const INTERVIEW_DURATION = 40 * 60 // 40分钟，单位：秒
const remainingTime = ref(INTERVIEW_DURATION)
let interviewTimer: number | null = null

// WebSocket配置（统一端口架构：使用FastAPI端口）
const websocketUrl = ref((() => {
  // 根据当前协议和主机自动生成WebSocket URL
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const host = window.location.hostname
  // 统一端口架构：WebSocket集成到FastAPI，使用API端口
  return `${protocol}//${host}:9005/ws/voice-interview`
})())

// 面试完成
const showCompletionDialog = ref(false)
const interviewDuration = ref<string>('')  // 面试时长（格式：分钟及其后两位）

// 生命周期
onMounted(async () => {
  console.log('VoiceInterviewStreaming mounted, invitationId:', invitationId)
  if (!invitationId) {
    ElMessage.error('缺少邀请ID，请重新登录')
    router.push('/interview/login')
    return
  }
  await loadQuestions()
})

onUnmounted(() => {
  if (countdownTimer) {
    clearInterval(countdownTimer)
  }
  if (interviewTimer) {
    clearInterval(interviewTimer)
  }
})

// 加载题目列表
const loadQuestions = async () => {
  try {
    console.log('开始加载题目列表, invitationId:', invitationId)
    // 响应拦截器已处理，response 就是数据对象
    const response = await voiceInterviewApi.getInterviewQuestions(invitationId)
    console.log('题目列表响应:', response)
    
    if (response && response.success) {
      // 后端已经按question_order排序（先基础题再专业题），这里再次排序确保正确
      // 注意：数据库中的类型是BASIC和SPECIALTY，不是BASIC_INFO和PROFESSIONAL
      questions.value = response.questions.sort((a: any, b: any) => {
        // 先按类型排序（基础题优先：BASIC或BASIC_INFO）
        const aIsBasic = a.question_type === 'BASIC' || a.question_type === 'BASIC_INFO'
        const bIsBasic = b.question_type === 'BASIC' || b.question_type === 'BASIC_INFO'
        if (aIsBasic !== bIsBasic) {
          return aIsBasic ? -1 : 1
        }
        // 再按question_order排序
        return (a.question_order || 0) - (b.question_order || 0)
      })
      
      console.log('排序后的题目列表:', questions.value.map((q: any, idx: number) => ({
        index: idx,
        question_id: q.question_id,
        question_type: q.question_type,
        question_order: q.question_order
      })))
      
      // 找到第一个未完成的题目
      const firstIncompleteIndex = questions.value.findIndex(
        (q: any) => q.session_status !== 'COMPLETED'
      )
      if (firstIncompleteIndex >= 0) {
        currentQuestionIndex.value = firstIncompleteIndex
        console.log('当前题目索引:', currentQuestionIndex.value)
      } else if (questions.value.length > 0) {
        // 如果所有题目都完成了，显示最后一题
        currentQuestionIndex.value = questions.value.length - 1
      }
    } else {
      ElMessage.error(response?.message || '加载题目失败')
    }
  } catch (error: any) {
    console.error('加载题目失败:', error)
    ElMessage.error(error.response?.data?.detail || '加载题目失败，请检查网络连接')
  }
}

// 开始面试
const startInterview = async () => {
  if (!currentQuestion.value) {
    ElMessage.error('没有可用的题目')
    return
  }

  // 状态检查：如果正在切换中，不允许开始
  if (isSwitching.value) {
    console.warn('[开始面试] 正在切换题目中，请稍候')
    return
  }

  try {
    isStarting.value = true
    
    // 等待录音组件连接WebSocket并开始录音
    await new Promise(resolve => setTimeout(resolve, 500))
    
    if (recorderRef.value) {
      // 开始录音时，发送当前题目的questionId
      const currentQuestionId = currentQuestion.value?.question_id
      await recorderRef.value.startRecording()
      isRecording.value = true
      
      // 重置并启动面试倒计时
      remainingTime.value = INTERVIEW_DURATION
      startInterviewCountdown()
      
      ElMessage.success('面试已开始，请开始回答')
      
      // 如果录音组件支持，发送当前题目ID
      if (currentQuestionId && recorderRef.value.sendStartRecording) {
        // 注意：startRecording 内部已经会发送 start_recording 消息，这里不需要再发送
        console.log('[开始面试] 当前题目ID:', currentQuestionId)
      }
    }
  } catch (error: any) {
    console.error('开始面试失败:', error)
    ElMessage.error('开始面试失败，请重试')
  } finally {
    isStarting.value = false
  }
}

// 启动面试倒计时
const startInterviewCountdown = () => {
  if (interviewTimer) {
    clearInterval(interviewTimer)
  }
  
  interviewTimer = window.setInterval(() => {
    remainingTime.value--
    
    // 倒计时结束，自动结束面试
    if (remainingTime.value <= 0) {
      remainingTime.value = 0
      stopInterview()
      ElMessage.warning('面试时间已到，面试已自动结束')
      if (interviewTimer) {
        clearInterval(interviewTimer)
        interviewTimer = null
      }
    }
  }, 1000)
}

// 格式化倒计时显示：分钟:秒（如 40:00）
const formatCountdown = (seconds: number): string => {
  const minutes = Math.floor(seconds / 60)
  const secs = seconds % 60
  return `${minutes}:${String(secs).padStart(2, '0')}`
}

// 停止面试（与答完最后一题一致：走“面试已完成”弹窗，不再显示“开始面试”）
const stopInterview = async () => {
  // 停止倒计时
  if (interviewTimer) {
    clearInterval(interviewTimer)
    interviewTimer = null
  }
  if (recorderRef.value) {
    await recorderRef.value.stopRecording()
  }
  isRecording.value = false
  // 与答完最后一题相同：显示“面试已完成”弹窗
  await handleInterviewCompleted()
}

// 手动切换到下一题（添加防抖和状态检查）
const manualNextQuestion = () => {
  // 状态锁：如果正在切换中，直接返回
  if (isSwitching.value) {
    console.warn('[手动切换] 正在切换中，忽略本次操作')
    return
  }
  
  console.log('[手动切换] 用户点击"回答完毕"，发送切换请求')
  
  if (recorderRef.value && recorderRef.value.isRecording) {
    // 发送切换请求给后端，不在这里设置 lastSwitchTime
    // lastSwitchTime 应该在 switchToQuestion 成功切换后才设置
    recorderRef.value.manualNextQuestion()
  } else {
    ElMessage.warning('请先开始面试')
  }
}

// 取消自动切换
const cancelAutoAdvance = () => {
  showSilenceCountdown.value = false
  if (countdownTimer) {
    clearInterval(countdownTimer)
    countdownTimer = null
  }
  // 通知后端取消自动切换
  if (recorderRef.value) {
    recorderRef.value.cancelAutoAdvance()
  }
}

// 事件处理：接收来自录音组件的文本更新（参考老地址实现）
const handleTextUpdate = (text: string) => {
  // 如果切换题目后收到空文本，确保清空显示
  if (!text || text.trim() === '') {
    transcribedText.value = ''
    intermediateText.value = ''
    return
  }
  
  // 老地址的实现：直接使用传入的文本
  // 录音组件会发送完整的文本（accumulatedText + intermediateText）
  transcribedText.value = text
  
  // 如果有录音组件引用，尝试获取中间文本用于实时显示
  if (recorderRef.value) {
    try {
      intermediateText.value = recorderRef.value.intermediateText || ''
    } catch (error) {
      intermediateText.value = ''
    }
  } else {
    intermediateText.value = ''
  }
  
  // 有文本输入时取消倒计时
  if (showSilenceCountdown.value && (transcribedText.value || intermediateText.value)) {
    cancelAutoAdvance()
  }
}

const handleEvaluationUpdate = (evaluation: any) => {
  latestEvaluation.value = evaluation
}

const handleError = (error: string) => {
  ElMessage.error(error)
}

// 收到后端确认的题目切换（只更新UI，不再发送命令）
const handleQuestionSwitched = (data: { oldQuestionId: string, newQuestionId: string }) => {
  console.log('[题目切换] 收到后端确认:', data)
  // 只更新UI，不发送任何命令（避免循环）
  switchToQuestion(data.newQuestionId)
  // 切换题目时，清空追问问题
  followUpQuestion.value = null
}

// 处理追问问题触发
const handleFollowUpTrigger = (data: { question: string, is_follow_up: boolean, follow_up_question_id?: string, reason?: string }) => {
  console.log('[追问] 收到追问问题:', data)
  followUpQuestion.value = data.question
  
  // 显示特别提示
  ElMessage({
    message: '收到追问问题，请详细回答',
    type: 'warning',
    duration: 5000,
    showClose: true
  })
  
  // 清空当前转写文本，为追问答案做准备
  transcribedText.value = ''
  intermediateText.value = ''
}

// 收到下一题通知（可能是自动切换或手动切换）
const handleNextQuestion = async (data: { currentQuestionId: string, nextQuestionId: string, autoAdvanced: boolean }) => {
  console.log('[题目切换] 收到下一题通知:', data)
  
  if (data.autoAdvanced) {
    ElMessage({
      message: '检测到您已回答完毕，正在切换到下一题...',
      type: 'success',
      duration: 2000
    })
  }
  
  // 切换到新题目（只更新UI，不发送命令）
  if (data.nextQuestionId) {
    await switchToQuestion(data.nextQuestionId)
  } else {
    handleInterviewCompleted()
  }
}

const handleInterviewCompleted = async (data?: { durationMinutes?: number | null }) => {
  isRecording.value = false
  
  // 停止倒计时
  if (interviewTimer) {
    clearInterval(interviewTimer)
    interviewTimer = null
  }
  
  // 获取面试总时长
  try {
    if (data?.durationMinutes !== undefined && data.durationMinutes !== null) {
      // 使用后端返回的时长
      interviewDuration.value = `${data.durationMinutes}分钟`
    } else {
      // 计算已用时长（从开始面试到现在的时长）
      const usedTime = INTERVIEW_DURATION - remainingTime.value
      const durationMinutes = round(usedTime / 60, 2)
      interviewDuration.value = `${durationMinutes}分钟`
    }
  } catch (error) {
    console.error('获取面试时长失败:', error)
    interviewDuration.value = ''
  }
  
  showCompletionDialog.value = true
}

// 格式化时长：分钟及其后两位
const round = (value: number, decimals: number): number => {
  return Math.round(value * Math.pow(10, decimals)) / Math.pow(10, decimals)
}

const handleSilenceCountdown = (data: { countdown: number }) => {
  silenceCountdown.value = data.countdown
  showSilenceCountdown.value = true
  
  // 启动倒计时
  if (countdownTimer) {
    clearInterval(countdownTimer)
  }
  
  countdownTimer = window.setInterval(() => {
    silenceCountdown.value--
    if (silenceCountdown.value <= 0) {
      showSilenceCountdown.value = false
      if (countdownTimer) {
        clearInterval(countdownTimer)
        countdownTimer = null
      }
    }
  }, 1000)
}

// 切换到指定题目（防抖和状态锁）
let lastSwitchTime = 0
let lastSwitchQuestionId: string | null = null  // 记录上次切换的题目ID
const SWITCH_DEBOUNCE_MS = 300  // 0.3秒防抖（进一步降低防抖时间）

const switchToQuestion = async (questionId: string) => {
  // 状态锁：如果正在切换中，直接返回
  if (isSwitching.value) {
    console.warn('[题目切换] 状态锁：正在切换中，忽略本次切换')
    return
  }
  
  const index = questions.value.findIndex((q: any) => q.question_id === questionId)
  if (index < 0) {
    console.warn('[题目切换] 题目不存在:', questionId)
    console.warn('[题目切换] 当前题目列表:', questions.value.map((q: any) => ({ id: q.question_id, type: q.question_type, order: q.question_order })))
    return
  }
  
  // 调试日志：显示题目信息
  console.log(`[题目切换] 查找题目: questionId=${questionId}, 找到索引=${index}`)
  console.log(`[题目切换] 题目信息:`, {
    questionId,
    index,
    questionType: questions.value[index]?.question_type,
    questionOrder: questions.value[index]?.question_order,
    currentIndex: currentQuestionIndex.value,
    currentQuestionId: questions.value[currentQuestionIndex.value]?.question_id
  })
  
  // 如果切换到的是当前题目，直接返回
  if (index === currentQuestionIndex.value) {
    console.log('[题目切换] 已是当前题目，无需切换')
    return
  }
  
  // 防抖：如果切换到的是上次切换的题目，且时间间隔很短，则忽略
  // 但如果是不同的题目，允许切换（可能是后端返回的切换确认）
  const now = Date.now()
  if (lastSwitchQuestionId === questionId && lastSwitchTime > 0 && now - lastSwitchTime < SWITCH_DEBOUNCE_MS) {
    console.warn('[题目切换] 防抖：重复切换到同一题目，忽略本次切换', {
      lastSwitchQuestionId,
      questionId,
      timeDiff: now - lastSwitchTime
    })
    return
  }
  
  // 如果是不同的题目，清除防抖状态（允许切换）
  if (lastSwitchQuestionId !== questionId) {
    console.log('[题目切换] 切换到不同题目，清除防抖状态', {
      lastSwitchQuestionId,
      questionId
    })
    lastSwitchTime = 0  // 重置防抖时间
  }
  
  try {
    isSwitching.value = true
    lastSwitchTime = now
    lastSwitchQuestionId = questionId
    
    console.log(`[题目切换] 开始切换: ${currentQuestionIndex.value} -> ${index}, questionId: ${questionId}`)
    
    // 重置状态
    transcribedText.value = ''
    intermediateText.value = ''
    latestEvaluation.value = null
    
    // 延迟切换以显示动画
    await new Promise(resolve => setTimeout(resolve, 300))
    
    currentQuestionIndex.value = index
    
    // 关键：收到后端确认消息后，只更新UI，不再发送任何命令
    // 避免循环触发：后端发送确认 → 前端更新UI → 前端不应该再发送命令
    console.log('[题目切换] UI切换完成，不发送命令')
    
    // 切换题目时，清空追问问题
    followUpQuestion.value = null
    
  } catch (error) {
    console.error('[题目切换] 切换失败:', error)
  } finally {
    // 延迟释放状态锁，防止快速连续切换
    setTimeout(() => {
      isSwitching.value = false
    }, 200)
  }
}

// 返回
const goBack = () => {
  router.push('/interview/login')
}
</script>

<style scoped lang="scss">
.streaming-interview {
  min-height: 100vh;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  display: flex;
  flex-direction: column;
}

.interview-progress-bar {
  padding: 20px 40px;
  background: rgba(255, 255, 255, 0.1);
  backdrop-filter: blur(10px);
  
  .progress-info {
    display: flex;
    align-items: center;
    gap: 20px;
    
    .question-counter {
      color: white;
      font-size: 18px;
      font-weight: 600;
      min-width: 120px;
    }
    
    :deep(.el-progress) {
      flex: 1;
      
      .el-progress-bar__outer {
        background: rgba(255, 255, 255, 0.2);
      }
    }
  }
  
  .interview-countdown {
    margin-top: 10px;
    
    :deep(.el-alert) {
      background: rgba(255, 255, 255, 0.15);
      border: 1px solid rgba(255, 255, 255, 0.3);
      
      .el-alert__title {
        color: white;
        font-size: 16px;
        font-weight: 600;
      }
      
      .countdown-text {
        font-size: 20px;
        font-weight: 700;
        font-family: 'Courier New', monospace;
        color: white;
      }
    }
    
    :deep(.el-alert--error) {
      background: rgba(245, 108, 108, 0.3);
      border-color: rgba(245, 108, 108, 0.5);
      animation: pulse 1s infinite;
    }
    
    :deep(.el-alert--warning) {
      background: rgba(230, 162, 60, 0.3);
      border-color: rgba(230, 162, 60, 0.5);
    }
  }
}

@keyframes pulse {
  0%, 100% {
    opacity: 1;
  }
  50% {
    opacity: 0.7;
  }
}

.completion-content {
  text-align: center;
  padding: 40px 20px;
  
  .completion-message {
    .success-icon {
      color: #67c23a;
      margin-bottom: 20px;
    }
    
    h2 {
      font-size: 28px;
      color: #303133;
      margin: 20px 0 10px;
      font-weight: 600;
    }
    
    .completion-text {
      font-size: 16px;
      color: #606266;
      margin: 10px 0 30px;
    }
    
    .duration-info {
      margin-top: 20px;
      
      .duration-text {
        font-size: 18px;
        color: #606266;
        margin: 20px 0;
        
        .duration-value {
          font-size: 24px;
          font-weight: 700;
          color: #409eff;
          margin-left: 8px;
        }
      }
    }
  }
}

.interview-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 40px;
  transition: opacity 0.3s ease;
  
  &.switching {
    opacity: 0.5;
  }
}

.question-container {
  text-align: center;
  max-width: 900px;
  width: 100%;
  margin-bottom: 40px;
  
  .follow-up-notice {
    margin-bottom: 20px;
    
    .follow-up-hint {
      margin: 8px 0 0 0;
      font-size: 14px;
      color: #e6a23c;
      font-weight: 500;
    }
  }
  
  .question-text.follow-up-question {
    color: #e6a23c;
    border-left: 4px solid #e6a23c;
    padding-left: 20px;
    text-align: left;
  }
  
  .question-header {
    display: flex;
    justify-content: center;
    gap: 12px;
    margin-bottom: 24px;
  }
  
  .question-text {
    font-size: 32px;
    font-weight: 600;
    color: white;
    line-height: 1.6;
    margin: 0 0 40px 0;
    text-shadow: 0 2px 10px rgba(0, 0, 0, 0.2);
    
    &.follow-up-question {
      color: #ffd04b;
      border-left: 4px solid #e6a23c;
      padding-left: 20px;
      text-align: left;
      background: rgba(230, 162, 60, 0.1);
      padding: 20px;
      border-radius: 8px;
    }
  }
  
  .recording-indicator {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 12px;
    margin-top: 30px;
    
    .wave-animation {
      display: flex;
      gap: 4px;
      align-items: center;
      height: 40px;
      
      span {
        width: 4px;
        background: white;
        border-radius: 2px;
        animation: wave 1.2s ease-in-out infinite;
        
        @for $i from 1 through 5 {
          &:nth-child(#{$i}) {
            animation-delay: #{$i * 0.1}s;
            height: #{20 + $i * 4}px;
          }
        }
      }
    }
    
    .recording-text {
      color: white;
      font-size: 16px;
      font-weight: 500;
    }
  }
}

@keyframes wave {
  0%, 100% {
    transform: scaleY(0.5);
    opacity: 0.7;
  }
  50% {
    transform: scaleY(1);
    opacity: 1;
  }
}

.transcription-container {
  max-width: 900px;
  width: 100%;
  margin-bottom: 30px;
  background: rgba(255, 255, 255, 0.95);
  border-radius: 12px;
  padding: 24px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.1);
  
  .transcription-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 16px;
    
    h3 {
      margin: 0;
      color: #303133;
      font-size: 18px;
      font-weight: 600;
    }
  }
  
  .transcription-text {
    min-height: 100px;
    max-height: 300px;
    overflow-y: auto;
    padding: 15px;
    background: #f5f5f5;
    border-radius: 8px;
    line-height: 1.8;
    font-size: 14px;
    
    .final-text {
      color: #333;
      white-space: pre-wrap;
      word-wrap: break-word;
    }
    
    .intermediate-text {
      color: #999;
      font-style: italic;
      white-space: pre-wrap;
      word-wrap: break-word;
    }
    
    .empty-text {
      color: #999;
      text-align: center;
      padding: 20px;
    }
  }
}

.silence-countdown {
  max-width: 900px;
  width: 100%;
  margin-bottom: 20px;
  
  :deep(.el-alert) {
    display: flex;
    align-items: center;
    justify-content: space-between;
    
    .el-alert__content {
      flex: 1;
    }
  }
}

.control-buttons {
  display: flex;
  gap: 20px;
  margin-top: 30px;
  
  .el-button {
    padding: 16px 32px;
    font-size: 18px;
    
    i {
      margin-right: 8px;
    }
  }
}

.completion-content {
  text-align: center;
  padding: 20px;
}

// 题目切换动画
.question-fade-enter-active,
.question-fade-leave-active {
  transition: all 0.4s ease;
}

.question-fade-enter-from {
  opacity: 0;
  transform: translateY(20px);
}

.question-fade-leave-to {
  opacity: 0;
  transform: translateY(-20px);
}

// 倒计时动画
.countdown-fade-enter-active,
.countdown-fade-leave-active {
  transition: all 0.3s ease;
}

.countdown-fade-enter-from,
.countdown-fade-leave-to {
  opacity: 0;
  transform: translateY(-10px);
}

.loading-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  color: white;
  gap: 20px;
  
  p {
    margin: 0;
    font-size: 18px;
  }
}

// 响应式设计
@media (max-width: 768px) {
  .interview-progress-bar {
    padding: 15px 20px;
    
    .progress-info {
      flex-direction: column;
      gap: 12px;
      
      .question-counter {
        font-size: 16px;
      }
    }
  }
  
  .interview-main {
    padding: 20px;
  }
  
  .question-container {
    .question-text {
      font-size: 24px;
    }
  }
  
  .control-buttons {
    flex-direction: column;
    width: 100%;
    
    .el-button {
      width: 100%;
    }
  }
}
</style>
