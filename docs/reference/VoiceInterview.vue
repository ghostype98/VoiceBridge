<template>
  <div class="voice-interview">
    <!-- 面试头部信息 -->
    <div class="interview-header">
      <el-card shadow="never">
        <el-descriptions :column="2" border>
          <el-descriptions-item label="候选人">{{ candidateInfo.candidate_name }}</el-descriptions-item>
          <el-descriptions-item label="岗位">{{ candidateInfo.position }}</el-descriptions-item>
          <el-descriptions-item label="部门">{{ candidateInfo.department }}</el-descriptions-item>
          <el-descriptions-item label="面试形式">{{ candidateInfo.interview_form }}</el-descriptions-item>
        </el-descriptions>
      </el-card>
    </div>

    <!-- 题目列表 -->
    <div class="questions-section">
      <el-card shadow="never">
        <template #header>
          <div class="questions-header">
            <span>面试题目 ({{ questions.length }}道)</span>
            <el-progress
              :percentage="completionProgress"
              :status="completionProgress === 100 ? 'success' : 'normal'"
              style="width: 200px"
            />
          </div>
        </template>

        <div class="questions-list">
          <el-timeline>
            <el-timeline-item
              v-for="(question, index) in questions"
              :key="question.question_id"
              :timestamp="`第${question.question_order}题`"
              :color="getQuestionColor(question)"
              size="large"
            >
              <div class="question-item">
                <div class="question-content">
                  <h4>{{ question.question_text }}</h4>
                  <div class="question-meta">
                    <el-tag size="small" :type="question.question_type === 'BASIC' ? 'success' : 'warning'">
                      {{ question.question_type === 'BASIC' ? '基础题' : '专业题' }}
                    </el-tag>
                    <el-tag size="small">{{ question.question_category }}</el-tag>
                    <el-tag size="small" type="info">预计{{ question.estimated_duration }}秒</el-tag>
                    <el-tag size="small" :type="getDifficultyColor(question.difficulty)">
                      {{ getDifficultyText(question.difficulty) }}
                    </el-tag>
                  </div>
                </div>

                <div class="question-actions">
                  <el-button
                    v-if="question.session_status === 'NOT_STARTED'"
                    type="primary"
                    @click="startQuestion(question)"
                  >
                    开始回答
                  </el-button>

                  <el-button
                    v-else-if="question.session_status === 'IN_PROGRESS'"
                    type="warning"
                    @click="continueQuestion(question)"
                  >
                    继续回答
                  </el-button>

                  <el-button
                    v-else-if="question.session_status === 'COMPLETED'"
                    type="success"
                    disabled
                  >
                    已完成
                  </el-button>

                  <div class="question-status" v-if="question.has_answer">
                    <el-tag size="small" type="success">已回答</el-tag>
                    <span class="follow-up-info" v-if="question.follow_up_used > 0">
                      追问 {{ question.follow_up_used }}/{{ question.follow_up_limit }}
                    </span>
                  </div>
                </div>
              </div>
            </el-timeline-item>
          </el-timeline>
        </div>
      </el-card>
    </div>

    <!-- 语音录制弹窗 -->
    <el-dialog
      v-model="recordingDialogVisible"
      title="语音回答"
      width="800px"
      :close-on-click-modal="false"
      :close-on-press-escape="false"
      @close="stopRecording"
    >
      <div v-if="currentQuestion" class="recording-content">
        <!-- 题目显示 -->
        <div class="question-display">
          <h3>{{ currentQuestion.question_text }}</h3>
          <div class="question-info">
            <el-tag :type="currentQuestion.question_type === 'BASIC' ? 'success' : 'warning'">
              {{ currentQuestion.question_type === 'BASIC' ? '基础题' : '专业题' }}
            </el-tag>
            <el-tag>{{ currentQuestion.question_category }}</el-tag>
          </div>
        </div>

        <!-- 语音录制组件 -->
        <VoiceInterviewRecorder
          ref="recorderRef"
          :invitation-id="invitationId"
          :question-id="currentQuestion.question_id"
          :websocket-url="websocketUrl"
          @text-update="handleTextUpdate"
          @evaluation-update="handleEvaluationUpdate"
          @error="handleError"
          @question-switched="handleQuestionSwitched"
          @next-question="handleNextQuestion"
          @interview-completed="handleInterviewCompleted"
        />
        
        <!-- 智能提示 -->
        <el-alert
          v-if="recordingDialogVisible && recorderRef?.isRecording"
          title="智能流式面试模式"
          type="info"
          :closable="false"
          show-icon
          style="margin-top: 16px"
        >
          <template #default>
            <p>系统会自动检测您的回答完成情况：</p>
            <ul style="margin: 8px 0; padding-left: 20px;">
              <li>连续静音3秒以上且回答完整时，将自动切换到下一题</li>
              <li>说出"回答完毕"、"下一题"等关键词时，将立即切换</li>
              <li>您也可以点击"回答完毕"按钮手动切换</li>
            </ul>
          </template>
        </el-alert>

        <!-- 转写结果显示 -->
        <div class="transcription-result" v-if="transcribedText">
          <h4>回答内容：</h4>
          <div class="transcription-text">{{ transcribedText }}</div>
        </div>

        <!-- 实时评价显示 -->
        <div class="evaluation-result" v-if="latestEvaluation">
          <h4>AI评价：</h4>
          <div class="evaluation-content">
            <div class="score-display">
              <el-rate
                v-model="latestEvaluation.score"
                disabled
                show-score
                :max="100"
                :colors="['#F56C6C', '#E6A23C', '#67C23A']"
              />
            </div>
            <p class="evaluation-reason">{{ latestEvaluation.reason }}</p>
          </div>
        </div>

        <!-- 追问提示 -->
        <el-alert
          v-if="followUpQuestion"
          title="AI建议追问"
          :description="followUpQuestion"
          type="warning"
          show-icon
          style="margin-top: 16px"
        />
      </div>

      <template #footer>
        <el-button @click="stopRecording">结束回答</el-button>
        <el-button type="primary" @click="completeQuestion">完成本题</el-button>
      </template>
    </el-dialog>

    <!-- 全局错误提示 -->
    <el-alert
      v-if="globalError"
      :title="globalError"
      type="error"
      :closable="false"
      show-icon
      style="margin-top: 16px"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import VoiceInterviewRecorder from '@/components/voice-interview/VoiceInterviewRecorder.vue'
import { voiceInterviewApi } from '@/api/modules/voice-interview'

// 路由
const route = useRoute()
const router = useRouter()

// Props
const invitationId = route.params.invitationId as string

// 状态
const candidateInfo = reactive({
  invitation_id: '',
  candidate_name: '',
  position: '',
  department: '',
  interview_form: ''
})

const questions = ref<any[]>([])
const currentQuestion = ref<any>(null)
const recordingDialogVisible = ref(false)
const recorderRef = ref()

// 转写和评价数据
const transcribedText = ref('')
const latestEvaluation = ref<any>(null)
const followUpQuestion = ref('')
const globalError = ref('')

// WebSocket配置
// WebSocket配置（统一端口架构：使用FastAPI端口）
const websocketUrl = ref((() => {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const host = window.location.hostname
  return `${protocol}//${host}:9005/ws/voice-interview`
})())

// 计算属性
const completionProgress = computed(() => {
  if (questions.value.length === 0) return 0
  const completedCount = questions.value.filter(q => q.session_status === 'COMPLETED').length
  return Math.round((completedCount / questions.value.length) * 100)
})

// 生命周期
onMounted(async () => {
  await loadUserInfo()
  await loadQuestions()
})

// 加载用户信息
const loadUserInfo = async () => {
  try {
    const userInfoStr = localStorage.getItem('interview_user')
    if (!userInfoStr) {
      ElMessage.error('用户信息不存在，请重新登录')
      router.push('/interview/login')
      return
    }

    const userInfo = JSON.parse(userInfoStr)
    Object.assign(candidateInfo, userInfo)

    // 验证邀请ID匹配
    if (userInfo.invitation_id !== invitationId) {
      ElMessage.error('邀请ID不匹配，请重新登录')
      router.push('/interview/login')
      return
    }

  } catch (error) {
    console.error('加载用户信息失败:', error)
    ElMessage.error('用户信息加载失败，请重新登录')
    router.push('/interview/login')
  }
}

// 加载题目列表
const loadQuestions = async () => {
  try {
    const response = await voiceInterviewApi.getInterviewQuestions(invitationId)

    // 响应拦截器已处理，response 就是数据对象
    if (response && response.success) {
      questions.value = response.questions
    } else {
      ElMessage.error(response?.message || '加载题目失败')
    }

  } catch (error: any) {
    console.error('加载题目失败:', error)
    ElMessage.error(error.response?.data?.detail || '加载题目失败')
  }
}

// 开始回答题目
const startQuestion = async (question: any) => {
  currentQuestion.value = question
  recordingDialogVisible.value = true

  // 重置状态
  transcribedText.value = ''
  latestEvaluation.value = null
  followUpQuestion.value = ''
  globalError.value = ''
}

// 继续回答题目
const continueQuestion = (question: any) => {
  startQuestion(question)
}

// 停止录制
const stopRecording = () => {
  if (recorderRef.value) {
    recorderRef.value.stopRecording()
  }
}

// 完成本题
const completeQuestion = () => {
  recordingDialogVisible.value = false
  currentQuestion.value = null

  // 重新加载题目列表以更新状态
  loadQuestions()
}

// 事件处理
const handleTextUpdate = (text: string) => {
  transcribedText.value = text
}

const handleEvaluationUpdate = (evaluation: any) => {
  latestEvaluation.value = evaluation

  // 检查是否需要追问
  if (evaluation.need_follow_up) {
    followUpQuestion.value = evaluation.follow_up_question
  } else {
    followUpQuestion.value = ''
  }
}

const handleError = (error: string) => {
  globalError.value = error
  ElMessage.error(error)
}

const handleQuestionSwitched = (data: { oldQuestionId: string, newQuestionId: string }) => {
  console.log('题目切换:', data)
  // 查找新题目并更新显示
  const newQuestion = questions.value.find(q => q.question_id === data.newQuestionId)
  if (newQuestion) {
    currentQuestion.value = newQuestion
    // 重置当前题目的转写文本
    transcribedText.value = ''
    latestEvaluation.value = null
    followUpQuestion.value = ''
    ElMessage.success('已切换到下一题')
  }
}

const handleNextQuestion = async (data: { currentQuestionId: string, nextQuestionId: string, autoAdvanced: boolean }) => {
  console.log('下一题:', data)
  
  if (data.autoAdvanced) {
    ElMessage({
      message: '检测到您已回答完毕，正在切换到下一题...',
      type: 'success',
      duration: 2000
    })
  }
  
  // 更新题目列表状态
  await loadQuestions()
  
  // 查找并切换到新题目
  const nextQuestion = questions.value.find(q => q.question_id === data.nextQuestionId)
  if (nextQuestion) {
    currentQuestion.value = nextQuestion
    transcribedText.value = ''
    latestEvaluation.value = null
    followUpQuestion.value = ''
    
    // 如果录音器还在运行，发送切换题目的命令
    if (recorderRef.value && recorderRef.value.isRecording) {
      // 通过recorder组件发送start_recording消息（但ASR会话不会重新创建）
      recorderRef.value.sendStartRecording(data.nextQuestionId)
    }
  } else {
    // 没有下一题，面试完成
    handleInterviewCompleted()
  }
}

const handleInterviewCompleted = () => {
  ElMessage.success('恭喜！您已完成所有面试题目')
  recordingDialogVisible.value = false
  currentQuestion.value = null
  loadQuestions()
}

// 工具函数
const getQuestionColor = (question: any) => {
  switch (question.session_status) {
    case 'COMPLETED': return '#67C23A'
    case 'IN_PROGRESS': return '#E6A23C'
    default: return '#909399'
  }
}

const getDifficultyColor = (difficulty: string) => {
  const colors: { [key: string]: string } = {
    'JUNIOR': 'success',
    'MIDDLE': 'warning',
    'SENIOR': 'danger'
  }
  return colors[difficulty] || 'info'
}

const getDifficultyText = (difficulty: string) => {
  const texts: { [key: string]: string } = {
    'JUNIOR': '初级',
    'MIDDLE': '中级',
    'SENIOR': '高级'
  }
  return texts[difficulty] || difficulty
}
</script>

<style scoped lang="scss">
.voice-interview {
  padding: 20px;
  max-width: 1200px;
  margin: 0 auto;
}

.interview-header {
  margin-bottom: 24px;
}

.questions-section {
  margin-bottom: 24px;
}

.questions-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  width: 100%;
}

.questions-list {
  max-height: 600px;
  overflow-y: auto;
}

.question-item {
  padding: 16px;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  background: #fafafa;
}

.question-content {
  margin-bottom: 12px;

  h4 {
    margin: 0 0 8px 0;
    color: #303133;
    font-size: 16px;
    line-height: 1.4;
  }

  .question-meta {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
}

.question-actions {
  display: flex;
  justify-content: space-between;
  align-items: center;

  .question-status {
    display: flex;
    align-items: center;
    gap: 8px;

    .follow-up-info {
      font-size: 12px;
      color: #909399;
    }
  }
}

.recording-content {
  .question-display {
    margin-bottom: 24px;
    padding: 16px;
    background: #f5f7fa;
    border-radius: 8px;

    h3 {
      margin: 0 0 12px 0;
      color: #303133;
      font-size: 18px;
      line-height: 1.4;
    }

    .question-info {
      display: flex;
      gap: 8px;
    }
  }

  .transcription-result,
  .evaluation-result {
    margin-top: 20px;
    padding: 16px;
    border: 1px solid #e4e7ed;
    border-radius: 8px;

    h4 {
      margin: 0 0 12px 0;
      color: #303133;
      font-size: 16px;
    }
  }

  .transcription-text {
    background: #f8f9fa;
    padding: 12px;
    border-radius: 4px;
    line-height: 1.6;
    color: #606266;
    min-height: 60px;
  }

  .evaluation-content {
    .score-display {
      margin-bottom: 12px;
    }

    .evaluation-reason {
      margin: 0;
      line-height: 1.5;
      color: #606266;
    }
  }
}

// 响应式设计
@media (max-width: 768px) {
  .voice-interview {
    padding: 10px;
  }

  .questions-header {
    flex-direction: column;
    gap: 12px;
    align-items: stretch;
  }

  .question-actions {
    flex-direction: column;
    gap: 8px;
    align-items: stretch;
  }

  .recording-content {
    .question-display h3 {
      font-size: 16px;
    }
  }
}
</style>