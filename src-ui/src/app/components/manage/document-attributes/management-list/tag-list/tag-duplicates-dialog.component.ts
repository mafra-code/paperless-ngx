import { Component, EventEmitter, Input, Output, inject } from '@angular/core'
import { NgbActiveModal } from '@ng-bootstrap/ng-bootstrap'
import { TagMergeGroup } from 'src/app/services/rest/tag.service'

@Component({
  selector: 'pngx-tag-duplicates-dialog',
  templateUrl: './tag-duplicates-dialog.component.html',
})
export class TagDuplicatesDialogComponent {
  activeModal = inject(NgbActiveModal)

  @Input()
  groups: TagMergeGroup[] = []

  @Output()
  mergeGroup = new EventEmitter<TagMergeGroup>()

  cancel(): void {
    this.activeModal.dismiss()
  }
}
