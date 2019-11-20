import React from 'react';
import { connect } from 'react-redux';
import { withStyles } from '@material-ui/core/styles';
import { Box, VBox } from 'react-layout-components';
const moment = require('moment');
import { 
    Table, TableRow, TableBody, TableCell,
    TableHead, TextField
} from '@material-ui/core';

import ApiClient from '../ApiClient';
import ItemDetails from '../ItemDetails';

const styles = theme => ({
    not_selected: {
        cursor: 'pointer'
    },
    selected: {
        backgroundColor: '#212121',
        cursor: 'pointer'
    }
});

class UserListComponent extends ApiClient {

    state = {
        users: [],
        filter: '',
        selected: null
    }

    componentDidMount = async() => {
        let userList = await this.apiFetch(`/user/${this.props.domain}/list`);
        if ([undefined, null, false].includes(userList)) return null;
        this.setState({
            users: userList.data
        });
    }

    isSelected = (item) => {
        const { classes } = this.props;
        if ([undefined, null].includes(this.state.selected)) {
            return classes.not_selected;
        }
        if (item[0] == this.state.selected[0]) {
            return classes.selected;
        } else {
            return classes.not_selected;
        }
    }

    selectUser = (item) => {
        if ([undefined, null].includes(this.state.selected)) {
            this.setState({ selected: item })
            return;
        }
        if (this.state.selected[0] == item[0]) {
            this.setState({ selected: null });
        } else {
            this.setState({ selected: item })
        }
    }

    renderUsers = () => {
        return this.state.users.map(row => {
            if (this.state.filter != '' && !row[2].includes(this.state.filter)) {
                return null;
            } 
            return (
                <TableRow
                    className={this.isSelected(row)}
                    onClick={ (e) => this.selectUser(row) }
                    key={row[0]}
                >
                    <TableCell>
                        {row[0]}
                    </TableCell>
                    <TableCell>
                        {row[2]}
                    </TableCell>
                    <TableCell>
                        {row[1]}
                    </TableCell>
                </TableRow>
            );
        });
    }

    render() {
        return (
            <VBox>
                <Box>
                    <TextField
                        fullWidth={true}
                        label="Filter by Name"
                        skeleton={this.props.skeleton}
                        value={this.state.filter}
                        onChange={ (e) => this.setState({ filter: e.target.value }) }
                    />
                </Box>
                <Box wrap>
                    <Box flex={3}>
                        <Table className="margin-top">
                            <TableHead>
                                <TableRow>
                                    <TableCell>ID</TableCell>     
                                    <TableCell>Name</TableCell>
                                    <TableCell>SID</TableCell>
                                </TableRow>
                            </TableHead>
                            <TableBody>
                                {this.renderUsers()}
                            </TableBody>
                        </Table>
                    </Box>
                    {this.state.selected && <Box flex={3} className="mbox pbox">
                        <ItemDetails
                            domain={this.props.domain}
                            type="user"
                            selection={this.state.selected}
                        />
                    </Box>}
                </Box>
            </VBox>
        );
    }
}

const mapStateToProps = (state) => {
    return {}
}

const mapDispatchToProps = (dispatch) => {
    return {}
}

const UserList = connect(mapStateToProps, mapDispatchToProps)(withStyles(styles, { withTheme: true })(UserListComponent));
export default UserList;
