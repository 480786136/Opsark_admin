<script setup>
import { nextTick, ref, watch } from "vue";
const props = defineProps({
  open: Boolean,
  title: String,
  busy: Boolean,
  wide: Boolean,
});
const emit = defineEmits(["update:open"]);
const element = ref(null);
watch(
  () => props.open,
  async (value) => {
    await nextTick();
    if (value && element.value && !element.value.open)
      element.value.showModal();
    if (!value && element.value?.open) element.value.close();
  },
  { immediate: true },
);
function close() {
  if (!props.busy) emit("update:open", false);
}
</script>
<template>
  <dialog
    ref="element"
    class="detail-dialog"
    :class="{ wide }"
    :aria-label="title"
    @cancel.prevent="close"
    @close="emit('update:open', false)"
  >
    <div class="dialog-heading">
      <h2>{{ title }}</h2>
      <button type="button" class="secondary" :disabled="busy" @click="close">
        关闭
      </button>
    </div>
    <div class="dialog-body"><slot /></div>
  </dialog>
</template>
