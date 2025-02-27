function openJoinGroupModal(groupId) {
    $('#joinGroupModal').data('group-id', groupId).modal('show');
}

function sendJoinRequest() {
    var groupId = $('#joinGroupModal').data('group-id');
    var password = $('#password').val();
    var data = {
        groupId: groupId,
        password: password
    };
    $.ajax({
        type: 'POST',
        url: `/api/join_group/${groupId}/${password}`,
        success: function (data) {
            $('#joinGroupModal').modal('hide');
            location.reload();  
        },
        error: function (data) {
            $('#modalError').text(data.responseJSON.result);
        }
    });
}