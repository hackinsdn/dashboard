// Open Modal to join a group
function joinGroup() {
    $('#joinGroupModal').modal('show');
}

function sendJoinRequest(groupId) {
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
