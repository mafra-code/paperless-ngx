import { Component, Input, inject } from '@angular/core'
import { NgbActiveModal } from '@ng-bootstrap/ng-bootstrap'

export interface TagMergeOption {
  id: number
  name?: string
  document_count?: number
}

@Component({
  selector: 'pngx-tag-merge-dialog',
  templateUrl: './tag-merge-dialog.component.html',
})
export class TagMergeDialogComponent {
  activeModal = inject(NgbActiveModal)

  @Input()
  tags: TagMergeOption[] = []

  @Input()
  survivorId: number

  get sources(): TagMergeOption[] {
    return this.tags.filter((tag) => tag.id !== this.survivorId)
  }

  cancel(): void {
    this.activeModal.dismiss()
  }

  confirm(): void {
    this.activeModal.close({
      target: this.survivorId,
      sources: this.sources.map((tag) => tag.id),
    })
  }
}
